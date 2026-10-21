"""Factures électroniques — extraction (Factur-X) et lecture (CII / UBL).

Parsing pur : aucun fournisseur, aucune clé, aucun réseau. ``source`` est le
nom nu d'un fichier envoyé à l'agent, ou un chemin qui se résout dans
``uploads/agent{root}/`` (même logique que ``ocr.py``). Les URL sont refusées.
"""

from __future__ import annotations

import io
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from logging import getLogger
from pathlib import Path

from pypdf import PdfReader

logger = getLogger(__name__)

_MAX_FILE_BYTES = 10 * 1024 * 1024
_FACTURX_NAME = "factur-x.xml"
# Noms de pièce jointe acceptés, par ordre de priorité (comparés sans casse).
_XML_NAMES = (_FACTURX_NAME, "zugferd-invoice.xml", "xrechnung.xml")

_PROFILES = {
    "urn:factur-x.eu:1p0:minimum": "MINIMUM",
    "urn:factur-x.eu:1p0:basicwl": "BASIC WL",
    "urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:basicwl": "BASIC WL",
    "urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:basic": "BASIC",
    "urn:cen.eu:en16931:2017": "EN 16931",
    "urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended": "EXTENDED",
}


class _SourceError(Exception):
    """Erreur de source, avec un message en français destiné à l'utilisateur."""


def _error(message: str) -> dict:
    return {"status": "error", "error_message": message}


def _too_big() -> _SourceError:
    return _SourceError(
        "Fichier trop volumineux : la limite est de 10 Mo, il n'a pas été lu."
    )


def _upload_file(source: str) -> tuple[Path | None, str]:
    """(chemin disque, nom d'upload) de ``source`` dans les uploads de l'agent.

    Le chemin est ``None`` en stockage S3 (pas de fichier local). Ne lit jamais
    le fichier : seuls resolve/is_file/stat sont employés.
    """
    from apowerb.artifacts.agent_uploads import is_bare_filename, is_s3_storage
    from apowerb.configs import paths
    from apowerb.core.invocation_context import get_root_agent_id

    root = get_root_agent_id()
    if not root.isdigit():
        raise _SourceError("Aucun agent n'est associé à cette demande : fichier introuvable.")
    base = (paths.uploads_dir() / f"agent{root}").resolve()
    if is_bare_filename(source):
        candidate = (base / source).resolve()
    else:
        candidate = Path(source).resolve()
    if candidate.parent != base:
        raise _SourceError(
            f"Fichier introuvable : « {source} » doit être un fichier envoyé à l'agent."
        )
    if candidate.is_file():
        return candidate, candidate.name
    if is_s3_storage():
        return None, candidate.name
    raise _SourceError(f"Fichier introuvable : « {source} ».")


def _load_upload(source: str) -> bytes:
    from apowerb.artifacts.agent_uploads import read_agent_upload
    from apowerb.core.invocation_context import get_root_agent_id

    path, name = _upload_file(source)
    if path is not None and path.stat().st_size > _MAX_FILE_BYTES:
        raise _too_big()
    content, available = read_agent_upload(f"agent{get_root_agent_id()}", name)
    if content is None:
        listing = f" Fichiers disponibles : {', '.join(available)}." if available else ""
        raise _SourceError(f"Fichier introuvable : « {source} ».{listing}")
    if len(content) > _MAX_FILE_BYTES:
        raise _too_big()
    return content


def _profile_from_root(root) -> str | None:
    """Profil Factur-X lu dans le contexte du document CII, ``None`` si absent/inconnu."""
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.rsplit("}", 1)[-1] == "GuidelineSpecifiedDocumentContextParameter":
            for child in el:
                if isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1] == "ID":
                    return _PROFILES.get((child.text or "").strip())
    return None


def _profile_of(xml: bytes) -> str | None:
    try:
        from defusedxml import ElementTree as SafeET

        return _profile_from_root(SafeET.fromstring(xml, forbid_dtd=True))
    except Exception:  # XML invalide ou refusé : l'extraction reste valable
        logger.debug("Profil Factur-X illisible", exc_info=True)
    return None


def _xml_from_pdf(data: bytes) -> tuple[str, bytes]:
    """(nom de la pièce jointe, octets XML d'origine) d'un PDF Factur-X déjà lu en mémoire."""
    try:
        reader = PdfReader(io.BytesIO(data))
        encrypted = reader.is_encrypted
    except Exception:
        logger.debug("PDF illisible", exc_info=True)
        raise _SourceError("Ce PDF est illisible ou corrompu.") from None
    if encrypted:
        try:
            opened = bool(reader.decrypt(""))  # mot de passe utilisateur vide : cas courant
        except Exception:
            logger.debug("Déchiffrement sans mot de passe impossible", exc_info=True)
            opened = False
        if not opened:
            raise _SourceError(
                "Ce PDF est chiffré (protégé par mot de passe) : "
                "impossible d'en extraire la facture."
            )
    try:
        attachments = reader.attachments
        names = list(attachments.keys())
    except Exception:
        logger.debug("PDF illisible", exc_info=True)
        raise _SourceError("Ce PDF est illisible ou corrompu.") from None

    chosen = None
    for wanted in _XML_NAMES:
        chosen = next((n for n in names if n == wanted), None) or next(
            (n for n in names if n.lower() == wanted), None
        )
        if chosen is not None:
            break
    if chosen is None:
        raise _SourceError(
            "Ce PDF n'est pas une facture électronique : "
            f"aucune pièce jointe « {_FACTURX_NAME} » trouvée."
        )
    try:
        contents = attachments[chosen]
        raw = bytes(contents[0] if isinstance(contents, list) else contents)
    except Exception:
        logger.debug("Pièce jointe illisible", exc_info=True)
        raise _SourceError(f"La pièce jointe « {chosen} » est illisible ou corrompue.") from None
    if len(raw) > _MAX_FILE_BYTES:
        raise _SourceError(
            f"Pièce jointe « {chosen} » trop volumineuse : la limite est de 10 Mo."
        )
    return chosen, raw


def tool_einvoice_extract(source: str) -> dict:
    """Extrait le XML attaché à un PDF Factur-X, tel quel, et indique son profil.

    Args:
        source: nom d'un PDF envoyé à l'agent (ex. ``facture.pdf``) ou chemin
            dans les fichiers de l'agent. Les URL ne sont pas acceptées ;
            taille maximale 10 Mo.

    Returns:
        ``{"status": "success", "xml": <XML décodé UTF-8, sans retouche>,
        "profile": "MINIMUM" | "BASIC WL" | "BASIC" | "EN 16931" | "EXTENDED" | None}``.
        Pièce jointe lue : ``factur-x.xml``, sinon ``zugferd-invoice.xml`` ou
        ``xrechnung.xml`` (nom sans casse), 10 Mo au plus une fois extraite.
        ou ``{"status": "error", "error_message": <explication en français>}``.
    """
    try:
        src = (source or "").strip() if isinstance(source, str) else ""
        if not src:
            return _error("Aucun fichier indiqué : donne le nom du PDF envoyé à l'agent.")
        if src.lower().startswith(("http://", "https://")):
            return _error(
                "Les URL ne sont pas acceptées : envoie le fichier PDF à l'agent "
                "puis donne son nom."
            )
        if Path(src).suffix.lower() != ".pdf":
            return _error("Seuls les fichiers .pdf sont acceptés pour l'extraction.")

        try:
            chosen, raw = _xml_from_pdf(_load_upload(src))
        except _SourceError as exc:
            return _error(str(exc))
        try:
            xml = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _error(
                f"La pièce jointe « {chosen} » est illisible ou corrompue (UTF-8 attendu)."
            )
        return {"status": "success", "xml": xml, "profile": _profile_of(raw)}
    except Exception:
        logger.exception("tool_einvoice_extract a échoué")
        return _error("Extraction impossible : erreur inattendue.")


# ── Lecture (parse) ───────────────────────────────────────────────────────────

_CII_NS = "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
_UBL_NS = {
    "Invoice": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "CreditNote": "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
}
_TYPES = {
    "380": "invoice", "384": "invoice", "386": "invoice", "389": "invoice",
    "393": "invoice", "381": "credit_note",
}
_AMOUNT = re.compile(r"[+-]?([0-9]{1,18}(\.[0-9]*)?|\.[0-9]+)")  # 18 chiffres entiers : quantize reste possible
_NO_LINES_PROFILES = ("MINIMUM", "BASIC WL")


def _local(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _kids(el, name):
    """Enfants directs de ``el`` dont le nom local est ``name``."""
    if el is None:
        return []
    return [c for c in el if _local(c) == name]


def _walk_all(el, *names):
    cur = [el] if el is not None else []
    for name in names:
        cur = [c for e in cur for c in _kids(e, name)]
    return cur


def _walk(el, *names):
    """Premier élément au bout du chemin de noms locaux (enfants directs), sinon None."""
    found = _walk_all(el, *names)
    return found[0] if found else None


def _text(el) -> str | None:
    if el is None or el.text is None:
        return None
    value = el.text.strip()
    return value or None


def _dec(el) -> Decimal | None:
    value = _text(el)
    if value is None or not _AMOUNT.fullmatch(value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _by_currency(elements, currency: str | None, attr_of=lambda e: e):
    """Élément dont le currencyID vaut la devise de la facture, sinon le premier."""
    if currency:
        for el in elements:
            target = attr_of(el)
            if target is not None and target.get("currencyID") == currency:
                return el
    return elements[0] if elements else None


def _iso_date(value: str | None, fmt: str | None = None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        iso = value
    elif re.fullmatch(r"\d{8}", value) and fmt in (None, "102"):
        iso = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    else:
        return None
    try:
        date.fromisoformat(iso)
    except ValueError:
        return None
    return iso


def _cii_date(parent) -> str | None:
    node = _walk(parent, "DateTimeString")
    if node is None:
        return None
    return _iso_date(_text(node), node.get("format"))


def _scheme_id(party, container: str, scheme: str) -> str | None:
    for box in _walk_all(party, container):
        for ident in _kids(box, "ID"):
            if ident.get("schemeID") == scheme and _text(ident):
                return _text(ident)
    return None


def _cii_party(party) -> dict:
    return {
        "name": _text(_walk(party, "Name")),
        "siren": _scheme_id(party, "SpecifiedLegalOrganization", "0002"),
        "vat_id": _scheme_id(party, "SpecifiedTaxRegistration", "VA"),
    }


def _doc_type(code_el) -> str | None:
    return _TYPES.get(_text(code_el) or "")


def _category(el) -> str:
    """Catégorie de TVA (BT-118 / BT-151) ; absente = S (taux normal)."""
    return (_text(el) or "S").upper()


def _parse_cii(root, profile: str | None) -> tuple[dict, dict]:
    doc = _walk(root, "ExchangedDocument")
    trade = _walk(root, "SupplyChainTradeTransaction")
    agreement = _walk(trade, "ApplicableHeaderTradeAgreement")
    settlement = _walk(trade, "ApplicableHeaderTradeSettlement")
    summ = _walk(settlement, "SpecifiedTradeSettlementHeaderMonetarySummation")
    currency = _text(_walk(settlement, "InvoiceCurrencyCode"))
    taxes = _walk_all(settlement, "ApplicableTradeTax")
    lines = []
    for item in _walk_all(trade, "IncludedSupplyChainTradeLineItem"):
        line_settle = _walk(item, "SpecifiedLineTradeSettlement")
        lines.append({
            "label": _text(_walk(item, "SpecifiedTradeProduct", "Name")),
            "quantity": _dec(_walk(item, "SpecifiedLineTradeDelivery", "BilledQuantity")),
            "unit_price": _dec(_walk(item, "SpecifiedLineTradeAgreement",
                                     "NetPriceProductTradePrice", "ChargeAmount")),
            "vat_rate": _dec(_walk(line_settle, "ApplicableTradeTax", "RateApplicablePercent")),
            "net_amount": _dec(_walk(line_settle, "SpecifiedTradeSettlementLineMonetarySummation",
                                     "LineTotalAmount")),
        })
    breakdown = [
        {"rate": _dec(_walk(t, "RateApplicablePercent")),
         "base": _dec(_walk(t, "BasisAmount")),
         "amount": _dec(_walk(t, "CalculatedAmount"))}
        for t in taxes
    ]
    extra = {
        "line_total": _dec(_walk(summ, "LineTotalAmount")),
        "line_total_present": _walk(summ, "LineTotalAmount") is not None,
        "vat_categories": [_category(_walk(t, "CategoryCode")) for t in taxes],
    }
    return {
        "number": _text(_walk(doc, "ID")),
        "issue_date": _cii_date(_walk(doc, "IssueDateTime")),
        "due_date": _cii_date(_walk(settlement, "SpecifiedTradePaymentTerms", "DueDateDateTime")),
        "type": _doc_type(_walk(doc, "TypeCode")),
        "currency": currency,
        "seller": _cii_party(_walk(agreement, "SellerTradeParty")),
        "buyer": _cii_party(_walk(agreement, "BuyerTradeParty")),
        "lines": lines,
        "vat_breakdown": breakdown,
        "totals": {
            "net": _dec(_walk(summ, "TaxBasisTotalAmount")),
            "vat": _dec(_by_currency(_kids(summ, "TaxTotalAmount"), currency)),
            "gross": _dec(_walk(summ, "GrandTotalAmount")),
            "due": _dec(_walk(summ, "DuePayableAmount")),
        },
        "iban": _text(_walk(settlement, "SpecifiedTradeSettlementPaymentMeans",
                            "PayeePartyCreditorFinancialAccount", "IBANID")),
        "source_format": "cii",
        "profile": profile,
    }, extra


def _ubl_party(wrapper) -> dict:
    party = _walk(wrapper, "Party")
    legal = _walk(party, "PartyLegalEntity")
    siren = next((_text(c) for c in _kids(legal, "CompanyID")
                  if c.get("schemeID") in (None, "0002") and _text(c)), None)
    schemes = _kids(party, "PartyTaxScheme")
    vat_scheme = next((s for s in schemes
                       if (_text(_walk(s, "TaxScheme", "ID")) or "").upper() == "VAT"),
                      schemes[0] if schemes else None)
    return {
        "name": _text(_walk(legal, "RegistrationName")),
        "siren": siren,
        "vat_id": _text(_walk(vat_scheme, "CompanyID")),
    }


def _parse_ubl(root) -> tuple[dict, dict]:
    credit = _local(root) == "CreditNote"
    totals = _walk(root, "LegalMonetaryTotal")
    currency = _text(_walk(root, "DocumentCurrencyCode"))
    subtotals = _walk_all(root, "TaxTotal", "TaxSubtotal")
    tax_total = _by_currency(_kids(root, "TaxTotal"), currency,
                             attr_of=lambda t: _walk(t, "TaxAmount"))
    lines = []
    for item in _kids(root, "CreditNoteLine" if credit else "InvoiceLine"):
        lines.append({
            "label": _text(_walk(item, "Item", "Name")),
            "quantity": _dec(_walk(item, "CreditedQuantity" if credit else "InvoicedQuantity")),
            "unit_price": _dec(_walk(item, "Price", "PriceAmount")),
            "vat_rate": _dec(_walk(item, "Item", "ClassifiedTaxCategory", "Percent")),
            "net_amount": _dec(_walk(item, "LineExtensionAmount")),
        })
    breakdown = [
        {"rate": _dec(_walk(s, "TaxCategory", "Percent")),
         "base": _dec(_walk(s, "TaxableAmount")),
         "amount": _dec(_walk(s, "TaxAmount"))}
        for s in subtotals
    ]
    extra = {
        "line_total": _dec(_walk(totals, "LineExtensionAmount")),
        "line_total_present": _walk(totals, "LineExtensionAmount") is not None,
        "vat_categories": [_category(_walk(s, "TaxCategory", "ID")) for s in subtotals],
    }
    if credit:
        due = _text(_walk(root, "PaymentMeans", "PaymentDueDate"))
    else:
        due = _text(_walk(root, "DueDate"))
    code = _walk(root, "CreditNoteTypeCode" if credit else "InvoiceTypeCode")
    return {
        "number": _text(_walk(root, "ID")),
        "issue_date": _iso_date(_text(_walk(root, "IssueDate"))),
        "due_date": _iso_date(due),
        "type": _doc_type(code),
        "currency": currency,
        "seller": _ubl_party(_walk(root, "AccountingSupplierParty")),
        "buyer": _ubl_party(_walk(root, "AccountingCustomerParty")),
        "lines": lines,
        "vat_breakdown": breakdown,
        "totals": {
            "net": _dec(_walk(totals, "TaxExclusiveAmount")),
            "vat": _dec(_walk(tax_total, "TaxAmount")),
            "gross": _dec(_walk(totals, "TaxInclusiveAmount")),
            "due": _dec(_walk(totals, "PayableAmount")),
        },
        "iban": _text(_walk(root, "PaymentMeans", "PayeeFinancialAccount", "ID")),
        "source_format": "ubl",
        "profile": None,
    }, extra


def _read_invoice(source) -> tuple[dict, dict] | dict:
    """(facture normalisée publique, données internes) ou dict d'erreur.

    Les données internes (BT-106, catégories de TVA) servent au contrôle et ne
    sont pas exposées par ``tool_einvoice_parse``.
    """
    src = (source or "").strip() if isinstance(source, str) else ""
    if not src:
        return _error("Aucun fichier indiqué : donne le nom de la facture envoyée à l'agent.")
    if src.lower().startswith(("http://", "https://")):
        return _error(
            "Les URL ne sont pas acceptées : envoie le fichier à l'agent "
            "puis donne son nom."
        )
    suffix = Path(src).suffix.lower()
    if suffix not in (".pdf", ".xml"):
        return _error("Seuls les fichiers .xml et .pdf (Factur-X) sont acceptés.")

    try:
        data = _load_upload(src)
        if suffix == ".pdf":
            _name, data = _xml_from_pdf(data)  # octets XML d'origine, sans réencodage
    except _SourceError as exc:
        return _error(str(exc))

    try:
        from defusedxml import ElementTree as SafeET

        root = SafeET.fromstring(data, forbid_dtd=True)
    except Exception:
        logger.debug("XML refusé ou illisible", exc_info=True)
        return _error(
            "Ce fichier n'est pas un XML valide, ou il contient une déclaration "
            "DOCTYPE / des entités, refusées par sécurité."
        )

    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    name = _local(root)
    if name == "CrossIndustryInvoice" and namespace == _CII_NS:
        return _parse_cii(root, _profile_from_root(root))
    if name in _UBL_NS and namespace == _UBL_NS[name]:
        return _parse_ubl(root)
    return _error("Format non reconnu : ce XML n'est ni une facture CII ni une facture UBL.")


def tool_einvoice_parse(source: str) -> dict:
    """Lit une facture électronique CII ou UBL (XML, ou PDF Factur-X) et la normalise.

    Args:
        source: nom d'un fichier .xml ou .pdf envoyé à l'agent (ex. ``facture.xml``)
            ou chemin dans les fichiers de l'agent. Les URL ne sont pas acceptées.

    Returns:
        ``{"status": "success", "invoice": {number, issue_date, due_date, type,
        currency, seller, buyer, lines, vat_breakdown, totals, iban,
        source_format, profile}}`` : montants en ``Decimal``, dates ISO, champ
        absent = ``None`` ; ``type`` vaut ``"invoice"`` (380/384/386/389/393), ``"credit_note"``
        (381) ou ``None``. En cas d'échec :
        ``{"status": "error", "error_message": <explication en français>}``.
    """
    try:
        read = _read_invoice(source)
        if isinstance(read, dict):
            return read
        return {"status": "success", "invoice": read[0]}
    except Exception:
        logger.exception("tool_einvoice_parse a échoué")
        return _error("Lecture impossible : erreur inattendue.")


# ── Contrôle partiel (check) ──────────────────────────────────────────────────

_RULES = [
    "BR-02", "BR-03", "BR-04", "BR-05", "BR-06", "BR-07", "BR-16",
    "BR-CO-10", "BR-CO-15", "BR-S-09", "FR-SIREN", "FR-TVA", "IBAN", "DATE-ECHEANCE",
]
_TOLERANCE = Decimal("0.01")
_CENT = Decimal("0.01")
_PARTIES = (("seller", "vendeur"), ("buyer", "acheteur"))
_MENTION = (
    f"Résultat d'un contrôle partiel ({len(_RULES)} règles) : seules ces règles "
    "sont vérifiées, l'absence d'erreur ne garantit pas l'acceptation de la "
    "facture par une plateforme ou un validateur officiel."
)


def _err(rule: str, field: str, read, expected, explanation: str, fix: str) -> dict:
    return {
        "règle": rule,
        "champ": field,
        "valeur_lue": read,
        "attendu": expected,
        "explication": explanation,
        "correction": fix,
    }


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _valid_siren(value) -> str | None:
    """SIREN à 9 chiffres d'un SIREN ou d'un SIRET (14 chiffres), clé de Luhn juste, sinon None."""
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9]{9}|[0-9]{14}", text) and _luhn_ok(text[:9]):
        return text[:9]
    return None


def _vat_key(siren: str) -> str:
    return f"{(12 + 3 * (int(siren) % 97)) % 97:02d}"


def _iban_ok(raw: str) -> bool:
    iban = re.sub(r"\s+", "", raw).upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}", iban):
        return False
    if iban.startswith("FR") and len(iban) != 27:
        return False
    moved = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _presence_checks(inv: dict) -> list[dict]:
    errors = []
    simple = [
        ("BR-02", "number", inv.get("number"), "le numéro de facture",
         "Renseigner le numéro unique de la facture (par exemple FA-2026-0001)."),
        ("BR-03", "issue_date", inv.get("issue_date"), "la date d'émission",
         "Renseigner la date d'émission de la facture au format AAAA-MM-JJ."),
        ("BR-05", "currency", inv.get("currency"), "la devise de la facture",
         "Renseigner le code devise de la facture (par exemple EUR)."),
    ]
    for rule, field, value, label, fix in simple:
        if value is None or not str(value).strip():
            errors.append(_err(rule, field, value, "valeur renseignée",
                               f"Il manque {label} : c'est une donnée obligatoire.", fix))
    if inv.get("type") is None:
        errors.append(_err(
            "BR-04", "type", None, "380, 384, 386, 389, 393 (facture) ou 381 (avoir)",
            "Le type de document est absent ou son code n'est pas un code reconnu "
            "(380, 384, 386, 389, 393 ou 381).",
            "Indiquer le code de type 380 pour une facture, 381 pour un avoir."))
    for key, label in _PARTIES:
        party = inv.get(key) or {}
        if party.get("name") is None or not str(party["name"]).strip():
            rule = "BR-06" if key == "seller" else "BR-07"
            errors.append(_err(
                rule, f"{key}.name ({label})", party.get("name"), "nom renseigné",
                f"Le nom du {label} est absent : c'est une donnée obligatoire.",
                f"Renseigner la raison sociale du {label}."))
    if not inv.get("lines") and inv.get("profile") not in _NO_LINES_PROFILES:
        errors.append(_err(
            "BR-16", "lines", None, "au moins une ligne",
            "La facture ne contient aucune ligne de facturation.",
            "Ajouter au moins une ligne de facture avec son montant net."))
    return errors


def _amount_checks(inv: dict, extra: dict) -> list[dict]:
    errors = []
    totals = inv.get("totals") or {}
    net, vat, gross = totals.get("net"), totals.get("vat"), totals.get("gross")
    lines = inv.get("lines") or []
    nets = [line.get("net_amount") for line in lines]
    # BR-CO-10 : somme des lignes = BT-106 (total des lignes, avant remises et
    # frais du document) ; à défaut de BT-106, le total HT. Sans ligne, ou BT-106
    # présent mais illisible : non évaluée.
    line_total = extra.get("line_total")
    unreadable_bt106 = extra.get("line_total_present") and line_total is None
    compared = net if line_total is None else line_total
    if (lines and compared is not None and not unreadable_bt106
            and inv.get("profile") not in _NO_LINES_PROFILES
            and all(n is not None for n in nets)):
        total = sum(nets, Decimal(0))
        if abs(compared - total) > _TOLERANCE:
            if line_total is None:
                field, label = "totals.net", "Le total HT de la facture"
                fix = f"Corriger le total HT à {_money(total)}, somme des lignes."
            else:
                field, label = "totaux : total des lignes (BT-106)", "Le total des lignes (BT-106)"
                fix = f"Corriger le total des lignes (BT-106) à {_money(total)}, somme des lignes."
            errors.append(_err(
                "BR-CO-10", field, compared, total,
                f"{label} doit être égal à la somme des montants nets des lignes.", fix))
    if None not in (net, vat, gross):
        expected = net + vat
        if abs(gross - expected) > _TOLERANCE:
            errors.append(_err(
                "BR-CO-15", "totals.gross", gross, expected,
                "Le total TTC doit être égal au total HT plus le total de TVA.",
                f"Corriger le total TTC à {_money(expected)}, soit HT plus TVA."))
    categories = extra.get("vat_categories") or []
    for i, row in enumerate(inv.get("vat_breakdown") or []):
        rate, base, amount = row.get("rate"), row.get("base"), row.get("amount")
        category = categories[i] if i < len(categories) else "S"
        if category != "S" or None in (rate, base, amount):
            continue
        expected = (base * rate / 100).quantize(_CENT, rounding=ROUND_HALF_UP)
        if abs(amount - expected) > _TOLERANCE:
            errors.append(_err(
                "BR-S-09", f"vat_breakdown[{i}].amount", amount, expected,
                f"Le montant de TVA du taux {rate} % doit être égal à la base multipliée par le taux.",
                f"Corriger le montant de TVA à {_money(expected)} "
                f"(base {_money(base)} × {rate} %)."))
    return errors


def _identifier_checks(inv: dict) -> list[dict]:
    errors = []
    for key, label in _PARTIES:
        party = inv.get(key) or {}
        siren = party.get("siren")
        if siren is not None and str(siren).strip() and _valid_siren(siren) is None:
            errors.append(_err(
                "FR-SIREN", f"{key}.siren ({label})", siren,
                "9 chiffres (ou SIRET à 14 chiffres) avec une clé de Luhn juste",
                f"Le SIREN du {label} doit comporter 9 chiffres (14 pour un SIRET) "
                "et respecter la clé de Luhn.",
                f"Vérifier le SIREN du {label} auprès de son extrait Kbis "
                "ou de l'annuaire des entreprises."))
    for key, label in _PARTIES:
        party = inv.get(key) or {}
        vat = party.get("vat_id")
        if vat is None or not str(vat).strip():
            continue
        value = re.sub(r"\s+", "", str(vat)).upper()
        if not value.startswith("FR"):
            continue
        match = re.fullmatch(r"FR([0-9A-Z]{2})([0-9]{9})", value)
        own = _valid_siren(party.get("siren"))
        if not match:
            right = (f"FR{_vat_key(own)}{own}" if own
                     else "FR + clé à 2 caractères + SIREN à 9 chiffres")
            errors.append(_err(
                "FR-TVA", f"{key}.vat_id ({label})", vat, right,
                f"Le numéro de TVA français du {label} est mal formé : il doit comporter "
                "FR, une clé à 2 caractères et un SIREN à 9 chiffres.",
                f"Utiliser le numéro de TVA {right}." if own
                else f"Saisir le numéro de TVA du {label} au format FR, clé, SIREN à 9 chiffres."))
            continue
        vat_key, vat_siren = match.groups()
        if own is not None and vat_siren != own:
            right = f"FR{_vat_key(own)}{own}"
            errors.append(_err(
                "FR-TVA", f"{key}.vat_id ({label})", vat, right,
                f"Le SIREN contenu dans le numéro de TVA du {label} ({vat_siren}) "
                f"n'est pas celui du {label} ({own}).",
                f"Utiliser le numéro de TVA {right}, ou corriger le SIREN du {label}."))
        elif vat_key.isdigit() and vat_key != _vat_key(vat_siren):
            # Clé alphanumérique (ancien régime) : pas de comparaison de clé.
            right = f"FR{_vat_key(vat_siren)}{vat_siren}"
            errors.append(_err(
                "FR-TVA", f"{key}.vat_id ({label})", vat, right,
                f"La clé du numéro de TVA français du {label} ne correspond pas à son SIREN.",
                f"Utiliser le numéro de TVA {right}."))
    iban = inv.get("iban")
    if iban is not None and str(iban).strip() and not _iban_ok(str(iban)):
        errors.append(_err(
            "IBAN", "iban", iban, "IBAN valide (longueur et clé modulo 97)",
            "L'IBAN indiqué est invalide : sa longueur ou sa clé de contrôle modulo 97 est fausse.",
            "Recopier l'IBAN depuis le relevé d'identité bancaire de l'entreprise."))
    return errors


def _due_date_check(inv: dict) -> list[dict]:
    issue, due = inv.get("issue_date"), inv.get("due_date")
    if issue and due and due < issue:
        return [_err(
            "DATE-ECHEANCE", "due_date", due, f"une date égale ou postérieure au {issue}",
            "La date d'échéance ne peut pas précéder la date d'émission de la facture.",
            f"Indiquer une échéance au {issue} ou après, ou corriger la date d'émission.")]
    return []


def _all_checks(inv: dict, extra: dict) -> list[dict]:
    errors = (_presence_checks(inv) + _amount_checks(inv, extra)
              + _identifier_checks(inv) + _due_date_check(inv))
    errors.sort(key=lambda e: _RULES.index(e["règle"]))
    return errors


def tool_einvoice_check(source: str) -> dict:
    """Contrôle partiel d'une facture électronique : 14 règles nommées, erreurs en français.

    Sous-ensemble de règles EN 16931 (BR-02 à BR-07, BR-16, BR-CO-10, BR-CO-15,
    BR-S-09) plus SIREN (Luhn), TVA française, IBAN et échéance. Tolérance
    des montants : 0,01. Une donnée absente rend la règle non évaluée.

    Args:
        source: nom d'un fichier .xml ou .pdf (Factur-X) envoyé à l'agent, ou
            chemin dans les fichiers de l'agent. Les URL ne sont pas acceptées.

    Returns:
        ``{"status": "success", "errors": [{règle, champ, valeur_lue, attendu,
        explication, correction}], "rules": [...], "mention": ...}`` ou
        ``{"status": "error", "error_message": <explication en français>}``.
    """
    try:
        read = _read_invoice(source)
        if isinstance(read, dict):
            return read
        errors = _all_checks(*read)
        return {"status": "success", "errors": errors, "rules": list(_RULES), "mention": _MENTION}
    except Exception:
        logger.exception("tool_einvoice_check a échoué")
        return _error("Contrôle impossible : erreur inattendue.")


# ── Table d'un lot (to_table) ─────────────────────────────────────────────────

_COLUMNS = ["numero", "date", "vendeur", "siren", "ht", "tva", "ttc",
            "echeance", "nb_erreurs", "statut"]
_MAX_ROWS = 200


def _cell(value):
    return None if value is None else str(value)


def _unreadable_row() -> dict:
    row = dict.fromkeys(_COLUMNS)
    row["nb_erreurs"] = 0
    row["statut"] = "illisible"
    return row


def _table_row(source) -> dict:
    try:
        read = _read_invoice(source)
        if isinstance(read, dict):
            return _unreadable_row()
        inv, extra = read
        errors = _all_checks(inv, extra)
        seller = inv.get("seller") or {}
        totals = inv.get("totals") or {}
        return {
            "numero": inv.get("number"),
            "date": inv.get("issue_date"),
            "vendeur": seller.get("name"),
            "siren": seller.get("siren"),
            "ht": _cell(totals.get("net")),
            "tva": _cell(totals.get("vat")),
            "ttc": _cell(totals.get("gross")),
            "echeance": inv.get("due_date"),
            "nb_erreurs": len(errors),
            "statut": "erreurs" if errors else "ok",
        }
    except Exception:
        logger.exception("Ligne de table impossible")
        return _unreadable_row()


def tool_einvoice_to_table(sources: list[str]) -> dict:
    """Met un lot de factures électroniques à plat : une ligne par facture, colonnes fixes.

    Chaque facture (XML CII/UBL ou PDF Factur-X) est lue puis contrôlée. Une
    facture illisible donne une ligne de statut « illisible » sans arrêter le lot.
    Au plus 200 lignes : au-delà, les sources en trop sont ignorées et
    ``output_truncated`` vaut ``True``.

    Args:
        sources: liste de noms de fichiers envoyés à l'agent (upload, ex.
            ``["a.xml", "b.pdf"]``) ou de chemins dans les fichiers de l'agent.
            Les URL ne sont pas acceptées.

    Returns:
        Dict avec ``status`` ``"success"``, ``columns`` (numero, date, vendeur,
        siren, ht, tva, ttc, echeance, nb_erreurs, statut), ``rows`` (une ligne
        par source, dans l'ordre), ``output_truncated`` (bool) et ``mention``
        (contrôle partiel). ``statut`` vaut ``"ok"`` (aucune des 14 règles en
        défaut), ``"erreurs"`` ou ``"illisible"``. En cas d'échec :
        ``status`` ``"error"`` et ``error_message`` en français.
    """
    try:
        if isinstance(sources, str):
            sources = [sources]
        if not isinstance(sources, (list, tuple)) or not sources:
            return _error("Aucune source indiquée : donne la liste des fichiers de factures envoyés à l'agent.")
        truncated = len(sources) > _MAX_ROWS
        rows = [_table_row(s) for s in list(sources)[:_MAX_ROWS]]
        return {"status": "success", "columns": list(_COLUMNS), "rows": rows,
                "output_truncated": truncated, "mention": _MENTION}
    except Exception:
        logger.exception("tool_einvoice_to_table a échoué")
        return _error("Construction de la table impossible : erreur inattendue.")
