"""9788937461798 / 9791197671708 / 9791124065495 테스트."""
import asyncio, os, sys
from pathlib import Path

os.environ["ALLOW_INSECURE_SSL_FALLBACK"] = "true"
os.environ["INSECURE_SSL_FALLBACK_HOSTS_CSV"] = "www.aladin.co.kr"
sys.path.insert(0, str(Path(__file__).parent))

import httpx
from app.config import Settings
from app.fetcher import fetch_aladin_for_653
from app.kpipa_client import fetch_secondary_metadata_hint
from app.metadata_merge import merge_aladin_with_nlk
from app import ai_service
from app.ai_service import build_marc_653_line

TEST_ISBNS = [
    "9788953153172", "9791165047306", "9791165047207", "9788984817173", "9791172740962",
    "9791157958009", "9791167072399", "9791194440253", "9791124013915", "9791185062570",
]

async def run():
    settings = Settings(allow_insecure_ssl_fallback=True,
                        insecure_ssl_fallback_hosts_csv="www.aladin.co.kr")
    lines = []
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        for isbn in TEST_ISBNS:
            base, _ = await fetch_aladin_for_653(isbn, settings=settings, include_debug=True, client=client)
            hint, hint_src, _ = await fetch_secondary_metadata_hint(isbn, settings=settings, client=client)
            merge_src = "kpipa" if hint_src == "kpipa" else "none"
            meta = merge_aladin_with_nlk(base, hint, settings=settings, secondary_source=merge_src)
            raw_line, err, usage, quality = await ai_service.generate_653_subfield_line(
                meta, max_keywords=7, min_keywords=3, settings=settings)
            tag = build_marc_653_line(raw_line) if raw_line else "(오류)"
            kws = " / ".join(raw_line.split("$a")[1:]) if raw_line else ""
            lines.append(f"[{isbn}] {meta.title}")
            lines.append(f"분류: {meta.category}")
            lines.append(f"desc({len(meta.description)}) toc({len(meta.toc)}) pub_desc({len(meta.publisher_desc)})")
            lines.append(f"tag: {tag}")
            lines.append(f"keywords: {kws}")
            if quality:
                lines.append(f"ai_raw:{quality.ai_raw_count} filtered:{quality.filtered_count} final:{quality.final_count} flags:{quality.flags}")
            if usage:
                lines.append(f"tokens input:{usage.prompt_tokens} output:{usage.completion_tokens}")
            if err:
                lines.append(f"ERROR: {err}")
            lines.append("")
    out = "\n".join(lines)
    Path("output.txt").write_text(out, encoding="utf-8")

asyncio.run(run())
