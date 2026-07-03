import os
import requests


class SupabaseUpsertPipeline:
    """Upsert priced rows into public.live_prices_cache, batched.

    Conflict key matches the edge function's writer: (pip_code, supplier_name,
    branch_id). No-ops (JSONL only) when SUPABASE_URL / SUPABASE_SERVICE_KEY are
    unset. Only rows with a price are emitted by the spider.
    """

    BATCH = 200

    def open_spider(self, spider):
        s = spider.settings
        self.url = (s.get("SUPABASE_URL") or os.getenv("SUPABASE_URL", "")).rstrip("/")
        self.key = s.get("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_KEY", "")
        self.enabled = bool(self.url and self.key)
        self.buffer, self.written, self.errors = [], 0, 0
        if not self.enabled:
            spider.logger.warning("Supabase disabled (no URL/key) — writing JSONL only.")

    def process_item(self, item, spider):
        if self.enabled:
            self.buffer.append(dict(item))
            if len(self.buffer) >= self.BATCH:
                self._flush(spider)
        return item

    def close_spider(self, spider):
        if self.enabled:
            self._flush(spider)
            spider.logger.info("live_prices_cache: %d rows written, %d errors.", self.written, self.errors)

    def _flush(self, spider):
        if not self.buffer:
            return
        ep = f"{self.url}/rest/v1/live_prices_cache?on_conflict=pip_code,supplier_name,branch_id"
        h = {
            "apikey": self.key, "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        try:
            r = requests.post(ep, headers=h, json=self.buffer, timeout=60)
            if r.status_code >= 300:
                self.errors += 1
                spider.logger.error("live_prices_cache upsert %s: %s", r.status_code, r.text[:400])
            else:
                self.written += len(self.buffer)
        except Exception as exc:
            self.errors += 1
            spider.logger.error("live_prices_cache upsert error: %s", exc)
        finally:
            self.buffer = []
