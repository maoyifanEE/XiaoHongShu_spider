from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import load_config
from .crawler import Crawler
from .db import Database
from .exporter import export_excel
from .navigation_probe import run_navigation_probe
from .qa import run_offline_qa
from .utils import ensure_dirs, setup_logging


def main(argv: list[str] | None = None) -> int:
    base_dir = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="小红书指定博主公开信息采集与 Excel 导出工具")
    parser.add_argument("--mode", choices=["collect", "smoke", "login-only", "export-only", "qa-only", "navigation-probe"], default="collect")
    parser.add_argument("--smoke", action="store_true", help="等价于 --mode smoke")
    parser.add_argument("--login-only", action="store_true", help="等价于 --mode login-only")
    parser.add_argument("--export-only", action="store_true", help="等价于 --mode export-only")
    parser.add_argument("--qa-only", action="store_true", help="等价于 --mode qa-only")
    parser.add_argument("--resume", action="store_true", help="显式从最近 checkpoint 续跑，默认不恢复旧 checkpoint")
    parser.add_argument("--creator", help="按昵称、小红书号、user_id 或 URL 过滤指定博主")
    parser.add_argument("--max-notes", type=int, help="本次最多采集多少篇笔记")
    parser.add_argument("--config", type=Path, help="配置文件路径")
    parser.add_argument("--navigation-probe", action="store_true", help="Equivalent to --mode navigation-probe")
    args = parser.parse_args(argv)
    if args.smoke:
        args.mode = "smoke"
    if args.login_only:
        args.mode = "login-only"
    if args.export_only:
        args.mode = "export-only"
    if args.qa_only:
        args.mode = "qa-only"
    if args.navigation_probe:
        args.mode = "navigation-probe"

    ensure_dirs(base_dir)
    logger = setup_logging(base_dir)
    logger.info("START mode=%s base_dir=%s", args.mode, base_dir)
    config = load_config(base_dir, args.config)
    if args.mode == "navigation-probe":
        result = asyncio.run(run_navigation_probe(config, logger, args.creator))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        logger.info("EXIT")
        return 0 if all(item.get("success_count", 0) > 0 for item in result.values()) else 2
    db = Database(base_dir / "data" / "xhs_data.sqlite3")
    try:
        backup = db.backup_before_migration(base_dir / "data" / "backups")
        if backup:
            logger.info("DB_BACKUP path=%s", backup)
        db.migrate()
        logger.info("DB_READY path=%s", db.path)
        creators = [c for c in config.creators if c.enabled and (not args.creator or args.creator in " ".join(str(x) for x in [c.name, c.xhs_id, c.user_id, c.url] if x))]
        if not creators:
            raise ValueError("没有匹配且启用的 creator")
        if args.mode == "export-only":
            outputs = []
            for creator in creators:
                creator_id = creator.user_id or creator.url.rstrip("/").split("/")[-1]
                outputs.append(str(export_excel(db, base_dir, creator_id, creator.name, logger)))
            print(json.dumps({"mode": args.mode, "outputs": outputs}, ensure_ascii=False, indent=2))
            return 0
        if args.mode == "qa-only":
            reports = {}
            for creator in creators:
                creator_id = creator.user_id or creator.url.rstrip("/").split("/")[-1]
                reports[creator.name] = run_offline_qa(db, creator_id, logger)
            print(json.dumps(reports, ensure_ascii=False, indent=2))
            return 0 if all(report["passed"] for report in reports.values()) else 2
        crawler = Crawler(config, db, logger)
        result = asyncio.run(crawler.run(args.mode, args.creator, args.max_notes, resume=args.resume))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        db.close()
        logger.info("EXIT")


if __name__ == "__main__":
    sys.exit(main())
