import asyncio
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import logging
from ..config import settings

logger = logging.getLogger(__name__)

class DatabasePool:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DatabasePool, cls).__new__(cls)
            cls._instance.engine = None
            cls._instance.session_factory = None
            cls._instance._initialized = False
            cls._instance._loop = None
        return cls._instance

    async def initialize(self):
        """Initialize database connection pool"""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if self._initialized and self.session_factory and self._loop == current_loop:
            return

        if self.engine:
            try:
                await self.engine.dispose()
            except Exception:
                pass

        try:
            db_url = settings.database_url
            if db_url.startswith("postgresql://"):
                db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

            self.engine = create_async_engine(
                db_url,
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_max_overflow,
                pool_pre_ping=True,
                pool_recycle=settings.database_pool_recycle,
                echo=False
            )
            
            self.session_factory = async_sessionmaker(
                bind=self.engine,
                class_=AsyncSession,
                expire_on_commit=False
            )
            self._initialized = True
            self._loop = current_loop
            logger.info("✅ Database connection pool initialized")
        except Exception as e:
            logger.error(f"❌ Database pool initialization failed: {e}")
            self.engine = None
            self.session_factory = None
            self._initialized = False
            self._loop = None
            raise
    
    async def close(self):
        """Close database connections"""
        if self.engine:
            await self.engine.dispose()
    
    @asynccontextmanager
    async def get_session(self):
        """Get database session from pool as async context manager"""
        if not self.session_factory:
            raise Exception("Database pool not initialized")
        async with self.session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

# Global database pool instance
db_pool = DatabasePool()

async def get_db_session() -> AsyncSession:
    """Dependency to get database session"""
    async with db_pool.get_session() as session:
        yield session
