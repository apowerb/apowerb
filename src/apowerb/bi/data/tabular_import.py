"""Écriture d'une table chargée (xlsx, json, parquet…) comme jeu de données BI.

Le Data Pool et ``csv_executor`` ne lisent que du CSV : toute table non CSV
est convertie en CSV canonique (``helpers.tabular_loader.to_csv_bytes``) puis
rangée au même endroit et décrite par les mêmes métadonnées qu'un import CSV.
Partagé par ``POST /bi/upload-csv`` et par la prévision depuis une pièce
jointe du chat.
"""

from __future__ import annotations

from typing import Any

from apowerb.bi.data._bi_storage import save_file
from apowerb.helpers.tabular_loader import TabularData, to_csv_bytes

CSV_CONTENT_TYPE = "text/csv"


async def store_tabular_as_csv(
    data_store: Any,
    *,
    organization_id: str,
    project_id: str,
    file_id: str,
    name: str,
    data: TabularData,
    uploaded_by: str,
    extra_metadata: dict[str, Any] | None = None,
) -> str:
    """Range ``data`` en CSV et enregistre sa fiche ; renvoie la clé de stockage.

    Laisse remonter ``IntegrityError`` (nom déjà pris) à l'appelant.
    """
    s3_key = save_file(
        organization_id=organization_id,
        project_id=project_id,
        file_id=file_id,
        filename=f"{file_id}.csv",
        content=to_csv_bytes(data),
        content_type=CSV_CONTENT_TYPE,
    )
    await data_store.save(
        file_id=file_id,
        name=name,
        organization_id=organization_id,
        project_id=project_id,
        metadata={
            "s3_key": s3_key,
            "content_type": CSV_CONTENT_TYPE,
            "extension": ".csv",
            "row_count": len(data.rows),
            "columns": data.columns,
            "separator": ",",
            "uploaded_by": uploaded_by,
            **(extra_metadata or {}),
        },
    )
    return s3_key
