"""Factures électroniques SYNTHÉTIQUES pour les contrôles einvoice.

Tout est généré par le code : aucune entreprise, aucun SIREN ni IBAN réel de
client. Les identifiants valides sont calculés (Luhn, clé TVA FR, IBAN mod 97)
ou repris de l'IBAN d'exemple public de la norme (FR14 2004 1010 0505 0001 3M02 606).

Une facture est décrite par un dict Python (``facture()``), rendu en CII
(``cii``) ou en UBL (``ubl``) à partir du MÊME dict : la comparaison CII/UBL du
contrôle 8 porte donc sur la même facture. Une valeur ``None`` = élément absent
du XML. Montants en chaînes, recopiés tels quels dans le XML.
"""

from __future__ import annotations

import copy
import io
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

# ── Identifiants ────────────────────────────────────────────────────────────


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def siren(base8: str) -> str:
    """SIREN valide (clé de Luhn) à partir de 8 chiffres."""
    for d in "0123456789":
        if luhn_ok(base8 + d):
            return base8 + d
    raise AssertionError("inatteignable")


def siren_faux(valide: str) -> str:
    """Même SIREN avec une clé de Luhn fausse."""
    for d in "0123456789":
        cand = valide[:8] + d
        if not luhn_ok(cand):
            return cand
    raise AssertionError("inatteignable")


def tva_fr(siren_: str, decalage: int = 0) -> str:
    """Numéro de TVA FR : FR + clé (12 + 3 × (SIREN mod 97)) mod 97 + SIREN."""
    cle = ((12 + 3 * (int(siren_) % 97)) % 97 + decalage) % 97
    return f"FR{cle:02d}{siren_}"


def _iban_num(s: str) -> int:
    return int("".join(str(int(c, 36)) for c in s))


def iban_ok(iban: str) -> bool:
    s = iban.replace(" ", "").upper()
    return _iban_num(s[4:] + s[:4]) % 97 == 1


def iban_fr(bban: str) -> str:
    """IBAN FR valide pour un BBAN (23 caractères), clé calculée modulo 97."""
    cle = 98 - _iban_num(bban + "FR00") % 97
    return f"FR{cle:02d}{bban}"


IBAN_NORME = "FR1420041010050500013M02606"  # exemple public de la norme
# SIREN absents de l'annuaire des entreprises (recherche-entreprises.api.gouv.fr,
# base Sirene) au 22/10/2026 : 112233440, 556677888, 314159260, 271828188.
# Les anciens numéros de ces fixtures appartenaient à de vraies entreprises.
# Un numéro libre peut être attribué plus tard : revérifier avant toute publication.
SIREN_VENDEUR = siren("11223344")
SIREN_ACHETEUR = siren("55667788")
IBAN_CALCULE = iban_fr("30001000640000000000123")  # banque/compte fictifs

# ── Profils Factur-X (GuidelineSpecifiedDocumentContextParameter) ─────────────

URN_PROFILS = {
    "MINIMUM": "urn:factur-x.eu:1p0:minimum",
    "BASIC": "urn:cen.eu:en16931:2017#compliant#urn:factur-x.eu:1p0:basic",
    "EN 16931": "urn:cen.eu:en16931:2017",
    "EXTENDED": "urn:cen.eu:en16931:2017#conformant#urn:factur-x.eu:1p0:extended",
}

# ── Facture de référence ────────────────────────────────────────────────────


def facture(**changements) -> dict:
    """Facture correcte de référence ; ``changements`` remplace des clés de 1er niveau.

    Montants : 2 × 150 + 1 × 200 à 20 % ; 3 × 10 à 5,5 %.
    HT 530,00 ; TVA 100,00 + 1,65 = 101,65 ; TTC 631,65 ; net à payer 631,65.
    """
    base = {
        "number": "FA-2026-0042",
        "type_code": "380",
        "issue_date": "2026-10-01",
        "due_date": "2026-10-31",
        "currency": "EUR",
        "profile_urn": URN_PROFILS["EN 16931"],
        "seller": {
            "name": "Société Exemple Fictive SAS",
            "siren": SIREN_VENDEUR,
            "vat_id": tva_fr(SIREN_VENDEUR),
        },
        "buyer": {
            "name": "Atelier Imaginaire & Fils",
            "siren": SIREN_ACHETEUR,
            "vat_id": tva_fr(SIREN_ACHETEUR),
        },
        "lines": [
            {"label": "Prestation de conseil", "quantity": "2", "unit_price": "150.00",
             "vat_rate": "20.00", "net_amount": "300.00"},
            {"label": "Licence logicielle annuelle", "quantity": "1", "unit_price": "200.00",
             "vat_rate": "20.00", "net_amount": "200.00"},
            {"label": "Livre technique", "quantity": "3", "unit_price": "10.00",
             "vat_rate": "5.50", "net_amount": "30.00"},
        ],
        "vat_breakdown": [
            {"rate": "20.00", "base": "500.00", "amount": "100.00"},
            {"rate": "5.50", "base": "30.00", "amount": "1.65"},
        ],
        "totals": {"net": "530.00", "vat": "101.65", "gross": "631.65", "due": "631.65"},
        "iban": IBAN_NORME,
    }
    base = copy.deepcopy(base)
    base.update(copy.deepcopy(changements))
    return base


def facture_une_ligne(number: str, quantity: str, unit_price: str, rate: str, **changements) -> dict:
    """Facture correcte à une seule ligne ; montants calculés (arrondi au centime)."""
    net = (Decimal(quantity) * Decimal(unit_price)).quantize(Decimal("0.01"))
    tva = (net * Decimal(rate) / 100).quantize(Decimal("0.01"))
    data = facture(
        number=number,
        lines=[{"label": "Abonnement de maintenance", "quantity": quantity, "unit_price": unit_price,
                "vat_rate": rate, "net_amount": str(net)}],
        vat_breakdown=[{"rate": rate, "base": str(net), "amount": str(tva)}],
        totals={"net": str(net), "vat": str(tva), "gross": str(net + tva), "due": str(net + tva)},
    )
    data.update(copy.deepcopy(changements))
    return data


SIREN_VENDEUR_2 = siren("31415926")
SIREN_ACHETEUR_2 = siren("27182818")


def facture2() -> dict:
    """Deuxième facture correcte, différente de ``facture()`` sur tous les points.

    USD, une ligne 4 × 37,50 à 10 % : HT 150,00 ; TVA 15,00 ; TTC 165,00.
    """
    return facture_une_ligne(
        "INV-2026-0999", "4", "37.50", "10.00",
        issue_date="2026-11-15",
        due_date="2026-12-15",
        currency="USD",
        profile_urn=URN_PROFILS["BASIC"],
        seller={"name": "Bureau d'Études Fictif SARL", "siren": SIREN_VENDEUR_2,
                "vat_id": tva_fr(SIREN_VENDEUR_2)},
        buyer={"name": "Coopérative Imaginaire", "siren": SIREN_ACHETEUR_2,
               "vat_id": tva_fr(SIREN_ACHETEUR_2)},
        iban=IBAN_CALCULE,
    )


TYPES_DOCUMENT = {"380": "invoice", "384": "invoice", "381": "credit_note"}


def attendu(data: dict) -> dict:
    """Facture normalisée attendue de F2 (sans ``source_format`` ni ``profile``)."""

    def dec(v):
        return None if v is None else Decimal(v)

    return {
        "number": data["number"],
        "issue_date": data["issue_date"],
        "due_date": data["due_date"],
        "type": TYPES_DOCUMENT.get(data["type_code"]),
        "currency": data["currency"],
        "seller": dict(data["seller"]),
        "buyer": dict(data["buyer"]),
        "lines": [
            {k: (l[k] if k == "label" else dec(l[k]))
             for k in ("label", "quantity", "unit_price", "vat_rate", "net_amount")}
            for l in data["lines"]
        ],
        "vat_breakdown": [{k: dec(v[k]) for k in ("rate", "base", "amount")}
                          for v in data["vat_breakdown"]],
        "totals": {k: dec(data["totals"][k]) for k in ("net", "vat", "gross", "due")},
        "iban": data["iban"],
    }


# ── Rendu CII (Factur-X / ZUGFeRD, D16B) ─────────────────────────────────────


def _el(tag: str, value, attrs: str = "") -> str:
    if value is None:
        return ""
    return f"<{tag}{attrs}>{escape(str(value))}</{tag}>"


def _cii_date(tag: str, iso: str | None) -> str:
    if iso is None:
        return ""
    return (f'<{tag}><udt:DateTimeString format="102">{iso.replace("-", "")}'
            f"</udt:DateTimeString></{tag}>")


def _cii_party(tag: str, p: dict) -> str:
    out = [f"<{tag}>", _el("ram:Name", p.get("name"))]
    if p.get("siren") is not None:
        out.append("<ram:SpecifiedLegalOrganization>"
                   + _el("ram:ID", p["siren"], ' schemeID="0002"')
                   + "</ram:SpecifiedLegalOrganization>")
    if p.get("vat_id") is not None:
        out.append("<ram:SpecifiedTaxRegistration>"
                   + _el("ram:ID", p["vat_id"], ' schemeID="VA"')
                   + "</ram:SpecifiedTaxRegistration>")
    out.append(f"</{tag}>")
    return "".join(out)


def cii(data: dict) -> str:
    # Sans devise de facture (BR-05), aucun currencyID ne la laisse deviner.
    cur_attr = "" if data["currency"] is None else ' currencyID="' + data["currency"] + '"'
    lignes = []
    for i, l in enumerate(data["lines"], 1):
        lignes.append(
            "<ram:IncludedSupplyChainTradeLineItem>"
            f"<ram:AssociatedDocumentLineDocument><ram:LineID>{i}</ram:LineID></ram:AssociatedDocumentLineDocument>"
            f"<ram:SpecifiedTradeProduct>{_el('ram:Name', l['label'])}</ram:SpecifiedTradeProduct>"
            "<ram:SpecifiedLineTradeAgreement><ram:NetPriceProductTradePrice>"
            f"{_el('ram:ChargeAmount', l['unit_price'])}"
            "</ram:NetPriceProductTradePrice></ram:SpecifiedLineTradeAgreement>"
            "<ram:SpecifiedLineTradeDelivery>"
            + _el("ram:BilledQuantity", l["quantity"], ' unitCode="C62"') +
            "</ram:SpecifiedLineTradeDelivery>"
            "<ram:SpecifiedLineTradeSettlement>"
            "<ram:ApplicableTradeTax><ram:TypeCode>VAT</ram:TypeCode><ram:CategoryCode>S</ram:CategoryCode>"
            f"{_el('ram:RateApplicablePercent', l['vat_rate'])}</ram:ApplicableTradeTax>"
            "<ram:SpecifiedTradeSettlementLineMonetarySummation>"
            f"{_el('ram:LineTotalAmount', l['net_amount'])}"
            "</ram:SpecifiedTradeSettlementLineMonetarySummation>"
            "</ram:SpecifiedLineTradeSettlement>"
            "</ram:IncludedSupplyChainTradeLineItem>"
        )
    taxes = "".join(
        "<ram:ApplicableTradeTax>"
        f"{_el('ram:CalculatedAmount', v['amount'])}<ram:TypeCode>VAT</ram:TypeCode>"
        f"{_el('ram:BasisAmount', v['base'])}<ram:CategoryCode>S</ram:CategoryCode>"
        f"{_el('ram:RateApplicablePercent', v['rate'])}</ram:ApplicableTradeTax>"
        for v in data["vat_breakdown"]
    )
    paiement = ""
    if data["iban"] is not None:
        paiement = ("<ram:SpecifiedTradeSettlementPaymentMeans><ram:TypeCode>58</ram:TypeCode>"
                    "<ram:PayeePartyCreditorFinancialAccount>"
                    f"{_el('ram:IBANID', data['iban'])}"
                    "</ram:PayeePartyCreditorFinancialAccount></ram:SpecifiedTradeSettlementPaymentMeans>")
    echeance = ""
    if data["due_date"] is not None:
        echeance = ("<ram:SpecifiedTradePaymentTerms>"
                    + _cii_date("ram:DueDateDateTime", data["due_date"])
                    + "</ram:SpecifiedTradePaymentTerms>")
    t = data["totals"]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rsm:CrossIndustryInvoice'
        ' xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"'
        ' xmlns:qdt="urn:un:unece:uncefact:data:standard:QualifiedDataType:100"'
        ' xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"'
        ' xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">\n'
        "<rsm:ExchangedDocumentContext><ram:GuidelineSpecifiedDocumentContextParameter>"
        f"{_el('ram:ID', data['profile_urn'])}"
        "</ram:GuidelineSpecifiedDocumentContextParameter></rsm:ExchangedDocumentContext>\n"
        "<rsm:ExchangedDocument>"
        f"{_el('ram:ID', data['number'])}{_el('ram:TypeCode', data['type_code'])}"
        f"{_cii_date('ram:IssueDateTime', data['issue_date'])}"
        "</rsm:ExchangedDocument>\n"
        "<rsm:SupplyChainTradeTransaction>"
        + "".join(lignes)
        + "<ram:ApplicableHeaderTradeAgreement>"
        + _cii_party("ram:SellerTradeParty", data["seller"])
        + _cii_party("ram:BuyerTradeParty", data["buyer"])
        + "</ram:ApplicableHeaderTradeAgreement>"
        "<ram:ApplicableHeaderTradeDelivery/>"
        "<ram:ApplicableHeaderTradeSettlement>"
        + _el("ram:InvoiceCurrencyCode", data["currency"])
        + paiement + taxes + echeance
        + "<ram:SpecifiedTradeSettlementHeaderMonetarySummation>"
        f"{_el('ram:LineTotalAmount', t['net'])}{_el('ram:TaxBasisTotalAmount', t['net'])}"
        + _el("ram:TaxTotalAmount", t["vat"], cur_attr) +
        f"{_el('ram:GrandTotalAmount', t['gross'])}{_el('ram:DuePayableAmount', t['due'])}"
        "</ram:SpecifiedTradeSettlementHeaderMonetarySummation>"
        "</ram:ApplicableHeaderTradeSettlement>"
        "</rsm:SupplyChainTradeTransaction>\n"
        "</rsm:CrossIndustryInvoice>\n"
    )


# ── Rendu UBL 2.1 (Invoice, ou CreditNote pour le type 381) ──────────────────


def _ubl_party(tag: str, p: dict) -> str:
    out = [f"<cac:{tag}><cac:Party>"]
    # Enseigne commerciale (BT-28), TOUJOURS présente et différente du nom légal
    # (BT-27, RegistrationName) que la facture normalisée doit porter, comme
    # ram:Name en CII : sans nom légal, BR-06/BR-07 doivent tirer quand même.
    enseigne = "Enseigne " + (p.get("name") or tag)
    out.append(f"<cac:PartyName>{_el('cbc:Name', enseigne)}</cac:PartyName>")
    if p.get("vat_id") is not None:
        out.append("<cac:PartyTaxScheme>" + _el("cbc:CompanyID", p["vat_id"])
                   + "<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>")
    out.append("<cac:PartyLegalEntity>" + _el("cbc:RegistrationName", p.get("name"))
               + _el("cbc:CompanyID", p.get("siren"), ' schemeID="0002"')
               + "</cac:PartyLegalEntity>")
    out.append(f"</cac:Party></cac:{tag}>")
    return "".join(out)


def ubl(data: dict) -> str:
    avoir = data["type_code"] == "381"
    racine = "CreditNote" if avoir else "Invoice"
    ligne_tag = "CreditNoteLine" if avoir else "InvoiceLine"
    qte_tag = "CreditedQuantity" if avoir else "InvoicedQuantity"
    ca = "" if data["currency"] is None else f' currencyID="{data["currency"]}"'
    tete = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        (f'<{racine} xmlns="urn:oasis:names:specification:ubl:schema:xsd:{racine}-2"'
         ' xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"'
         ' xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">\n'),
        "<cbc:CustomizationID>urn:cen.eu:en16931:2017</cbc:CustomizationID>",
        _el("cbc:ID", data["number"]),
        _el("cbc:IssueDate", data["issue_date"]),
    ]
    if not avoir:
        tete.append(_el("cbc:DueDate", data["due_date"]))
    tete.append(_el("cbc:CreditNoteTypeCode" if avoir else "cbc:InvoiceTypeCode", data["type_code"]))
    tete.append(_el("cbc:DocumentCurrencyCode", data["currency"]))
    tete.append(_ubl_party("AccountingSupplierParty", data["seller"]))
    tete.append(_ubl_party("AccountingCustomerParty", data["buyer"]))
    if data["iban"] is not None or (avoir and data["due_date"] is not None):
        tete.append("<cac:PaymentMeans><cbc:PaymentMeansCode>58</cbc:PaymentMeansCode>"
                    + (_el("cbc:PaymentDueDate", data["due_date"]) if avoir else "")
                    + (f"<cac:PayeeFinancialAccount>{_el('cbc:ID', data['iban'])}</cac:PayeeFinancialAccount>"
                       if data["iban"] is not None else "")
                    + "</cac:PaymentMeans>")
    t = data["totals"]
    tete.append("<cac:TaxTotal>" + _el("cbc:TaxAmount", t["vat"], ca))
    for v in data["vat_breakdown"]:
        tete.append("<cac:TaxSubtotal>" + _el("cbc:TaxableAmount", v["base"], ca)
                    + _el("cbc:TaxAmount", v["amount"], ca)
                    + "<cac:TaxCategory><cbc:ID>S</cbc:ID>" + _el("cbc:Percent", v["rate"])
                    + "<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:TaxSubtotal>")
    tete.append("</cac:TaxTotal>")
    tete.append("<cac:LegalMonetaryTotal>" + _el("cbc:LineExtensionAmount", t["net"], ca)
                + _el("cbc:TaxExclusiveAmount", t["net"], ca)
                + _el("cbc:TaxInclusiveAmount", t["gross"], ca)
                + _el("cbc:PayableAmount", t["due"], ca) + "</cac:LegalMonetaryTotal>")
    for i, l in enumerate(data["lines"], 1):
        tete.append(
            f"<cac:{ligne_tag}><cbc:ID>{i}</cbc:ID>"
            + _el(f"cbc:{qte_tag}", l["quantity"], ' unitCode="C62"')
            + _el("cbc:LineExtensionAmount", l["net_amount"], ca)
            + "<cac:Item>" + _el("cbc:Name", l["label"])
            + "<cac:ClassifiedTaxCategory><cbc:ID>S</cbc:ID>" + _el("cbc:Percent", l["vat_rate"])
            + "<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory></cac:Item>"
            + "<cac:Price>" + _el("cbc:PriceAmount", l["unit_price"], ca) + "</cac:Price>"
            + f"</cac:{ligne_tag}>"
        )
    tete.append(f"\n</{racine}>\n")
    return "".join(tete)


# ── PDF ───────────────────────────────────────────────────────────────────────


def pdf_vierge(texte: str = "Facture fictive - document de test") -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(text=texte)
    return bytes(pdf.output())


def avec_pieces(pdf: bytes, pieces: dict[str, bytes]) -> bytes:
    """``pdf`` avec les fichiers ``pieces`` attachés (pièces jointes PDF)."""
    from pypdf import PdfWriter

    w = PdfWriter(clone_from=io.BytesIO(pdf))
    for nom, contenu in pieces.items():
        w.add_attachment(nom, contenu)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def facturx(xml: str | bytes) -> bytes:
    """PDF Factur-X : PDF généré par fpdf2 + XML attaché sous le nom factur-x.xml."""
    data = xml.encode("utf-8") if isinstance(xml, str) else xml
    return avec_pieces(pdf_vierge(), {"factur-x.xml": data})


def chiffrer(pdf: bytes, mot_de_passe: str = "secret-de-test") -> bytes:
    from pypdf import PdfWriter

    w = PdfWriter(clone_from=io.BytesIO(pdf))
    w.encrypt(user_password=mot_de_passe, owner_password="proprietaire", algorithm="RC4-128")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


@contextmanager
def espion_lectures(vise):
    """Enregistre toute ouverture d'un fichier pour lequel ``vise(chemin)`` est vrai.

    Intercepte builtins.open, io.open, os.open, Path.open et Path.read_bytes
    (l'ouverture a lieu quand même : on observe, on ne bloque pas). stat,
    exists, is_file et resolve ne sont pas des lectures.
    """
    import builtins
    import os
    import pathlib

    lectures: list[str] = []

    def cible(chemin) -> bool:
        try:
            return bool(vise(os.fspath(chemin)))
        except TypeError:  # descripteur entier, etc.
            return False

    vrais = {
        (builtins, "open"): builtins.open,
        (io, "open"): io.open,
        (os, "open"): os.open,
        (pathlib.Path, "open"): pathlib.Path.open,
        (pathlib.Path, "read_bytes"): pathlib.Path.read_bytes,
    }

    def enveloppe(nom, vrai, methode):
        if methode:
            def f(self, *a, **k):
                if cible(self):
                    lectures.append(f"{nom}({self})")
                return vrai(self, *a, **k)
        else:
            def f(chemin, *a, **k):
                if cible(chemin):
                    lectures.append(f"{nom}({chemin})")
                return vrai(chemin, *a, **k)
        return f

    for (obj, nom), vrai in vrais.items():
        setattr(obj, nom, enveloppe(nom, vrai, obj is pathlib.Path))
    try:
        yield lectures
    finally:
        for (obj, nom), vrai in vrais.items():
            setattr(obj, nom, vrai)


URLS = ["https://exemple.invalid/facture.xml", "http://exemple.invalid/facture.pdf"]


def sources_hors_dossier(agent_dir: Path, tmp_path: Path, ext: str, contenu: bytes):
    """Fichiers hors de uploads/agent42 et chemins qui y mènent (autre dossier,
    autre agent, ../, lien sortant). Rend (sources, vise) où ``vise`` reconnaît
    les fichiers hors dossier (pour l'espion)."""
    import os

    ailleurs = tmp_path / "ailleurs"
    autre = tmp_path / "agent7"
    for d in (ailleurs, autre):
        d.mkdir(exist_ok=True)
        (d / f"f{ext}").write_bytes(contenu)
    (agent_dir / f"lien{ext}").symlink_to(ailleurs / f"f{ext}")
    cibles = {str((ailleurs / f"f{ext}").resolve()), str((autre / f"f{ext}").resolve())}
    sources = [
        str(ailleurs / f"f{ext}"),
        str(autre / f"f{ext}"),
        f"{agent_dir}/../agent7/f{ext}",
        str(agent_dir / f"lien{ext}"),
    ]

    def vise(chemin: str) -> bool:
        return os.path.realpath(chemin) in cibles

    return sources, vise


def ecrire(dossier: Path, nom: str, contenu: str | bytes) -> str:
    """Écrit ``contenu`` dans ``dossier/nom`` et rend ``nom`` (nom d'upload nu)."""
    p = Path(dossier) / nom
    if isinstance(contenu, str):
        p.write_text(contenu, encoding="utf-8")
    else:
        p.write_bytes(contenu)
    return nom

