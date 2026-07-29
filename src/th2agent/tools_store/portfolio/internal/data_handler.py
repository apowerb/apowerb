"""
DataHandler class for querying datasets using DuckDB engine.
"""

import duckdb
from pathlib import Path
from typing import Optional, Union, List, Dict, Any
import pandas as pd


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

        # Load data based on file type
        if file_extension == ".csv":
            self.connection.execute(
                f"CREATE TABLE {table_name} AS SELECT * FROM read_csv_auto('{self.dataset_location}')"
            )
        elif file_extension == ".parquet":
            self.connection.execute(
                f"CREATE TABLE {table_name} AS SELECT * FROM read_parquet('{self.dataset_location}')"
            )
        elif file_extension == ".json":
            self.connection.execute(
                f"CREATE TABLE {table_name} AS SELECT * FROM read_json_auto('{self.dataset_location}')"
            )
        elif file_extension == ".arrow":
            self.connection.execute(
                f"CREATE TABLE {table_name} AS SELECT * FROM '{self.dataset_location}'"
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


# Example usage:
if __name__ == "__main__":
    # Example 1: Basic usage with context manager
    with DataHandler("path/to/data.csv") as handler:
        handler.load_dataset()

        # Query the data
        result = handler.query("SELECT * FROM data WHERE column > 10")
        print(result)

        # Get schema info
        schema = handler.get_schema()
        print(schema)

    # Example 2: Direct usage
    handler = DataHandler("path/to/data.parquet")
    handler.load_dataset(table_name="my_data")

    # Run aggregation query
    summary = handler.query(
        "SELECT AVG(value) as avg_value FROM my_data GROUP BY category"
    )
    print(summary)

    handler.close()
