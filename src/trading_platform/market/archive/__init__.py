from .models import Candle
from .index import (
    ARCHIVE_INDEX_FILENAME,
    ArchiveIndexError,
    build_archive_index,
    load_archive_index,
    verify_archive_index_files,
)
from .parquet import (
    ParquetCandleArchive,
    archive_root_from_catalog,
    create_duckdb_catalog,
)
from .vision import (
    ArchiveNotFoundError,
    BinanceFuturesMetadataFetcher,
    BinanceVisionHTTPFetcher,
    BinanceVisionWorkerPoolFetcher,
    DownloadProgress,
    DownloadResult,
    aggtrade_archive_url,
    download_history,
    kline_archive_url,
    monthly_aggtrade_archive_url,
    parse_aggtrade_archive,
    parse_kline_archive,
    parse_monthly_aggtrade_archive,
)

__all__ = [
    "ArchiveNotFoundError",
    "ARCHIVE_INDEX_FILENAME",
    "ArchiveIndexError",
    "BinanceFuturesMetadataFetcher",
    "Candle",
    "BinanceVisionHTTPFetcher",
    "BinanceVisionWorkerPoolFetcher",
    "DownloadResult",
    "DownloadProgress",
    "aggtrade_archive_url",
    "download_history",
    "kline_archive_url",
    "monthly_aggtrade_archive_url",
    "parse_aggtrade_archive",
    "parse_kline_archive",
    "parse_monthly_aggtrade_archive",
    "ParquetCandleArchive",
    "archive_root_from_catalog",
    "build_archive_index",
    "create_duckdb_catalog",
    "load_archive_index",
    "verify_archive_index_files",
]
