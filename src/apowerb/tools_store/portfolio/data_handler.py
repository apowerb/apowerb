"""
DataHandler class for querying datasets using DuckDB engine.
"""

import duckdb
from pathlib import Path
from typing import Optional, Union, List, Dict, Any
import pandas as pd
from apowerb.tools_store.portfolio.basic import tool_data_loader


class DataHandler:
    """
    A handler class for querying datasets using DuckDB engine.

    Supports multiple file formats: CSV, Parquet, JSON, Arrow, and more.
    """

    def __init__(self, dataset_location: Union[str, Path], read_only: bool = True):
        """
        Initialize DataHandler with a dataset location.

        Args:
            dataset_location: Path to the dataset file or directory
            read_only: Whether to open the database in read-only mode (default: True)
        """
        self.dataset_location = Path(dataset_location)
        self.connection: Optional[duckdb.DuckDBPyConnection] = None
        self.read_only = read_only
        self._table_name: Optional[str] = None

    def connect(self) -> None:
        """Establish connection to DuckDB engine."""
        if self.connection is None:
            self.connection = duckdb.connect(database=":memory:", read_only=False)

    def load_dataset(self, table_name: str = "data") -> None:
        """
        Load the dataset into DuckDB.

        Args:
            table_name: Name to use for the table in DuckDB (default: "data")
        """
        if self.connection is None:
            self.connect()

        if not self.dataset_location.exists():
            raise FileNotFoundError(f"Dataset not found: {self.dataset_location}")

        self._table_name = table_name
        file_extension = self.dataset_location.suffix.lower()

        # Convert path to use forward slashes for DuckDB compatibility
        file_path = str(self.dataset_location).replace("\\", "/")

        # Load data based on file type
        if file_extension == ".csv":
            self.connection.execute(
                f"CREATE TABLE {table_name} AS SELECT * FROM read_csv_auto('{file_path}')"
            )
        elif file_extension == ".parquet":
            self.connection.execute(
                f"CREATE TABLE {table_name} AS SELECT * FROM read_parquet('{file_path}')"
            )
        elif file_extension == ".json":
            self.connection.execute(
                f"CREATE TABLE {table_name} AS SELECT * FROM read_json_auto('{file_path}')"
            )
        elif file_extension == ".arrow":
            self.connection.execute(
                f"CREATE TABLE {table_name} AS SELECT * FROM '{file_path}'"
            )
        else:
            raise ValueError(f"Unsupported file format: {file_extension}")

    def query(self, sql: str) -> pd.DataFrame:
        """
        Execute a SQL query and return results as a pandas DataFrame.

        Args:
            sql: SQL query string

        Returns:
            Query results as pandas DataFrame
        """
        if self.connection is None:
            raise RuntimeError("Not connected. Call connect() or load_dataset() first.")

        result = self.connection.execute(sql).fetchdf()
        return result

    def query_dict(self, sql: str) -> List[Dict[str, Any]]:
        """
        Execute a SQL query and return results as a list of dictionaries.

        Args:
            sql: SQL query string

        Returns:
            Query results as list of dictionaries
        """
        df = self.query(sql)
        return df.to_dict("records")

    def get_schema(self) -> pd.DataFrame:
        """
        Get the schema of the loaded table.

        Returns:
            DataFrame with column names and types
        """
        if self._table_name is None:
            raise RuntimeError("No table loaded. Call load_dataset() first.")

        return self.query(f"DESCRIBE {self._table_name}")

    def get_table_info(self) -> Dict[str, Any]:
        """
        Get information about the loaded table.

        Returns:
            Dictionary with table statistics
        """
        if self._table_name is None:
            raise RuntimeError("No table loaded. Call load_dataset() first.")

        row_count = self.query(f"SELECT COUNT(*) as count FROM {self._table_name}")[
            "count"
        ][0]
        schema = self.get_schema()

        return {
            "table_name": self._table_name,
            "row_count": row_count,
            "column_count": len(schema),
            "columns": schema.to_dict("records"),
        }

    def head(self, n: int = 5) -> pd.DataFrame:
        """
        Get the first n rows of the dataset.

        Args:
            n: Number of rows to return (default: 5)

        Returns:
            DataFrame with first n rows
        """
        if self._table_name is None:
            raise RuntimeError("No table loaded. Call load_dataset() first.")

        return self.query(f"SELECT * FROM {self._table_name} LIMIT {n}")

    def execute(self, sql: str) -> None:
        """
        Execute a SQL statement without returning results.

        Args:
            sql: SQL statement to execute
        """
        if self.connection is None:
            raise RuntimeError("Not connected. Call connect() or load_dataset() first.")

        self.connection.execute(sql)

    def close(self) -> None:
        """Close the DuckDB connection."""
        if self.connection is not None:
            self.connection.close()
            self.connection = None
            self._table_name = None

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()

    def __del__(self):
        """Cleanup on deletion."""
        self.close()


def tool_load_and_query_data(table_name: str, sql_query: str) -> dict:
    """
    Utility function to load a dataset and execute a SQL query.

    Args:
        table_name: Name of the table to load and query
        sql_query: SQL query string to execute
    Returns:

        Query results as a dictionary with data and metadata
    """
    try:
        data_load_res = tool_data_loader(data_table=table_name)

        # Check if data loading was successful
        if not data_load_res.get("success", False):
            return {
                "success": False,
                "error": data_load_res.get("error", "Unknown error"),
                "message": data_load_res.get(
                    "message", "Failed to load data from storage"
                ),
            }

        raw_data_file = data_load_res.get("raw_data_file")
        if not raw_data_file:
            return {
                "success": False,
                "error": "No data file path returned",
                "message": "Data loader did not return a file path",
            }

        with DataHandler(raw_data_file) as handler:
            handler.load_dataset(table_name=table_name)
            df = handler.query(sql_query)

            # Convert DataFrame to serializable format
            return {
                "success": True,
                "data": df.to_dict("records"),
                "row_count": len(df),
                "column_count": len(df.columns),
                "columns": list(df.columns),
            }

    except FileNotFoundError as e:
        return {
            "success": False,
            "error": "FileNotFoundError",
            "message": f"Dataset file not found: {str(e)}",
        }
    except ValueError as e:
        return {
            "success": False,
            "error": "ValueError",
            "message": f"Invalid data or query: {str(e)}",
        }
    except Exception as e:
        return {
            "success": False,
            "error": type(e).__name__,
            "message": f"Error executing query: {str(e)}",
        }
