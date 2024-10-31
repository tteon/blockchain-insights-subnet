from datetime import datetime
from typing import Optional
from src.subnet.protocol import MODEL_KIND_BALANCE_TRACKING
from src.subnet.validator.validator import Validator


class BalanceTrackingQueryAPI:
    def __init__(self, validator: Validator):
        super().__init__()
        self.validator = validator

    async def _execute_query(self, network: str, query: str, model_kind = MODEL_KIND_BALANCE_TRACKING) -> dict:
        try:
            data = await self.validator.query_miner(network, model_kind, query, miner_key=None)
            return data
        except Exception as e:
            raise Exception(f"Error executing query: {str(e)}")

    async def get_balance_tracking(self,
                                   network: str,
                                   addresses: Optional[list[str]] = None,
                                   min_amount: Optional[int] = None,
                                   max_amount: Optional[int] = None,
                                   start_block_height: Optional[int] = None,
                                   end_block_height: Optional[int] = None,
                                   start_timestamp: Optional[int] = None,
                                   end_timestamp: Optional[int] = None,
                                   page: int = 1,
                                   page_size: int = 100
                                   ) -> dict:

        offset = (page - 1) * page_size
        conditions = []

        if addresses:
            formatted_addresses = ', '.join(f"'{address}'" for address in addresses)
            conditions.append(f"bc.address IN ({formatted_addresses})")

        if min_amount is not None:
            conditions.append(f"bc.d_balance >= {min_amount}")
        if max_amount is not None:
            conditions.append(f"bc.d_balance <= {max_amount}")

        if start_block_height is not None:
            conditions.append(f"bc.block >= {start_block_height}")
        if end_block_height is not None:
            conditions.append(f"bc.block <= {end_block_height}")

        if start_timestamp is not None:
            conditions.append(f"bc.block_timestamp >= {start_timestamp}")
        if end_timestamp is not None:
            conditions.append(f"bc.block_timestamp <= {end_timestamp}")

        where_clause = " AND ".join(conditions)
        if where_clause:
            where_clause = f"WHERE {where_clause}"

        query = f"""
            WITH result_set AS (
                SELECT 
                    bc.address,
                    bc.block as block_height,
                    bc.d_balance as balance_delta,
                    bc.block_timestamp as timestamp,
                    COUNT(*) OVER() as total_count
                FROM balance_changes bc
                {where_clause}
                ORDER BY bc.block_timestamp
                LIMIT {page_size}
                OFFSET {offset}
            )
            SELECT 
                json_build_object(
                    'response', (
                        SELECT json_agg(row_to_json(r))
                        FROM (
                            SELECT 
                                address,
                                block_height,
                                balance_delta,
                                timestamp
                            FROM result_set
                        ) r
                    ),
                    'total_items', COALESCE((SELECT total_count FROM result_set LIMIT 1), 0),
                    'total_pages', CEIL(COALESCE((SELECT total_count FROM result_set LIMIT 1), 0)::float / {page_size})
                ) as result;
        """

        result = await self._execute_query(network, query, model_kind=MODEL_KIND_BALANCE_TRACKING)
        return result[0]['result']

    async def get_balance_tracking_timestamp(self,
                                             network: str,
                                             start_date: Optional[str] = None,
                                             end_date: Optional[str] = None,
                                             page: int = 1,
                                             page_size: int = 100) -> dict:

        conditions = []
        if start_date:
            try:
                start_datetime = datetime.strptime(start_date, "%Y-%m-%d")
                conditions.append(f"timestamp >= '{start_datetime}'")
            except ValueError:
                raise ValueError("Invalid start_date format. Use YYYY-MM-DD.")

        if end_date:
            try:
                end_datetime = datetime.strptime(end_date, "%Y-%m-%d")
                conditions.append(f"timestamp <= '{end_datetime}'")
            except ValueError:
                raise ValueError("Invalid end_date format. Use YYYY-MM-DD.")

        where_clause = ""
        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)

        offset = (page - 1) * page_size
        query = f"""
            WITH result_set AS (
                SELECT 
                    block_height,
                    timestamp,
                    COUNT(*) OVER() as total_count
                FROM blocks
                {where_clause}
                ORDER BY timestamp
                LIMIT {page_size}
                OFFSET {offset}
            )
            SELECT 
                json_build_object(
                    'data', (
                        SELECT json_agg(row_to_json(r)) 
                        FROM (
                            SELECT block_height, timestamp 
                            FROM result_set
                        ) r
                    ),
                    'total_items', COALESCE((SELECT total_count FROM result_set LIMIT 1), 0),
                    'total_pages', CEIL(COALESCE((SELECT total_count FROM result_set LIMIT 1), 0)::float / {page_size})
                ) as response;
        """

        result = await self._execute_query(network, query)
        return result