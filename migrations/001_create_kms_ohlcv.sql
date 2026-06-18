CREATE TABLE IF NOT EXISTS kms_ohlcv (
  symbol VARCHAR(20) NOT NULL,
  trade_date DATE NOT NULL,

  open_price DECIMAL(20,4) NOT NULL,
  high_price DECIMAL(20,4) NOT NULL,
  low_price DECIMAL(20,4) NOT NULL,
  close_price DECIMAL(20,4) NOT NULL,
  volume BIGINT UNSIGNED NOT NULL,

  amount BIGINT UNSIGNED NULL,
  amount_source VARCHAR(20) NOT NULL DEFAULT 'computed',

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (symbol, trade_date),
  INDEX idx_kms_ohlcv_trade_date (trade_date),
  INDEX idx_kms_ohlcv_symbol_updated (symbol, updated_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS kms_ohlcv_cache_meta (
  symbol VARCHAR(20) NOT NULL,
  min_trade_date DATE NULL,
  max_trade_date DATE NULL,
  row_count INT UNSIGNED NOT NULL DEFAULT 0,
  last_cached_at DATETIME NOT NULL,

  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (symbol),
  INDEX idx_kms_ohlcv_cache_meta_last_cached_at (last_cached_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
