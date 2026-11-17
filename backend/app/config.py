"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    app_name: str = "分布式网页抓取调度平台"
    env: str = "local"
    port: int = 8000
    # 当前部署版本与水位迁移要求的版本；迁移预检会核对部署版本是否满足。
    app_version: str = "1.2.0"
    min_migration_version: str = "1.1.0"
    watermark_schema_version: str = "v2"
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200


settings = Settings()
