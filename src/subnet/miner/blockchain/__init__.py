import time
from src.subnet.validator.database import db_manager
from src.subnet.miner._config import MinerSettings
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from loguru import logger
from neo4j import READ_ACCESS, GraphDatabase
from neo4j.exceptions import Neo4jError


class GraphSearch:

    def __init__(self, settings: MinerSettings):
        self.driver = GraphDatabase.driver(
            settings.GRAPH_DATABASE_URL,
            auth=(settings.GRAPH_DATABASE_USER, settings.GRAPH_DATABASE_PASSWORD),
            connection_timeout=60,
            max_connection_lifetime=60,
            max_connection_pool_size=128,
            encrypted=False,
        )

    def execute_query(self, query: str):
        with self.driver.session(default_access_mode=READ_ACCESS) as session:
            try:
                result = session.run(query)
                if not result:
                    return None

                result_data = result.data()
                results_data = []

                for record in result_data:
                    processed_record = {}
                    for key, value in record.items():
                        # Check if the value is a node, relationship, or primitive value
                        if isinstance(value, dict) or hasattr(value, 'items'):
                            processed_record[key] = dict(value)
                        elif isinstance(value, str) or isinstance(value, (int, float)):
                            processed_record[key] = value
                        else:
                            # Handle relationships and their properties
                            if hasattr(value, "properties"):
                                processed_record[key] = dict(value.properties)
                            else:
                                processed_record[key] = value
                    results_data.append(processed_record)
                return results_data

            except Neo4jError as e:
                raise ValueError("Query attempted to modify data, which is not allowed.") from e

    def solve_challenge(self, in_total_amount: int, out_total_amount: int, tx_id_last_6_chars: str) -> str:
        """Solve a challenge and return the result."""

    def close(self):
        self.driver.close()


class BalanceSearch:

    async def execute_query(self, query: str):
        try:
            logger.info(f"Executing SQL query: {query}")
            async with db_manager.session() as session:
                async with session.begin():
                    await session.execute(text("SET TRANSACTION READ ONLY"))
                    result = await session.execute(text(query))
                    rows = result.fetchall()
                    columns = result.keys()
                    results = [dict(zip(columns, row)) for row in rows]
                    return results

        except SQLAlchemyError as e:
            logger.error(f"An error occurred: {str(e)}")
            raise ValueError("Query attempted to modify data, which is not allowed.") from e

    async def solve_challenge(self, block_heights: list[int]):
        start_time = time.time()
        try:
            logger.info(f"Executing balance sum query for block heights: {block_heights}")
            async with db_manager.session() as session:
                query = text("SELECT SUM(d_balance) FROM balance_changes WHERE block = ANY(:block_heights)")
                query = await session.execute(query, {'block_heights': block_heights})
                result = query.scalar()
                if result:
                    sum_d_balance = int(result)
                else:
                    sum_d_balance = 0

                logger.info(f"Balance sum for block heights {block_heights}: {sum_d_balance}")

                return sum_d_balance

        except SQLAlchemyError as e:
            logger.error(f"An error occurred: {str(e)}")
            return None
        finally:
            end_time = time.time()
            execution_time = end_time - start_time
            logger.info(f"Execution time for solve_challenge: {execution_time} seconds")
