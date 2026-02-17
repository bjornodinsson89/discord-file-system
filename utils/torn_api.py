"""Torn API utilities with rate limiting."""

import aiohttp
import asyncio
import time
import json
import logging
import socket
from typing import Dict, List, Optional, Tuple
from collections import deque
import config

log = logging.getLogger("happy_jumper.torn_api")


class TornAPIError(Exception):
    pass

class TornAPIRateLimitError(TornAPIError):
    pass

class TornAPIPermissionError(TornAPIError):
    pass


class RateLimiter:
    """Async rate limiter enforcing a rolling per-minute cap with burst control."""

    def __init__(self, max_per_minute: int = 100, burst: int = 10):
        self.max_per_minute = max_per_minute
        self.burst = burst
        self.requests = deque()
        self.lock = asyncio.Lock()
    
    async def acquire(self):
        """Wait for an available slot before allowing a Torn API request."""
        while True:
            async with self.lock:
                now = time.time()
                while self.requests and self.requests[0] < now - 60:
                    self.requests.popleft()
                recent = sum(1 for t in self.requests if t > now - 1)
                if recent >= self.burst:
                    sleep_time = 1
                elif len(self.requests) >= self.max_per_minute:
                    sleep_time = 60 - (now - self.requests[0])
                    if sleep_time < 0:
                        sleep_time = 0
                else:
                    self.requests.append(now)
                    return
            await asyncio.sleep(sleep_time)


class TornAPIClient:
    """Torn API client with global rate limiting and shared session management."""

    def __init__(self):
        self.rate_limiter = RateLimiter(
            config.API_RATE_LIMIT_PER_MINUTE,
            config.API_RATE_LIMIT_BURST
        )
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(family=socket.AF_INET, limit=50, ttl_dns_cache=300)
            headers = {"User-Agent": "HappyJumperBot/1.0 (aiohttp)"}
            self.session = aiohttp.ClientSession(connector=connector, headers=headers)
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def _request(self, path: str, params: Dict) -> Dict:
        if path.startswith("/v1") or "/v1" in path:
            raise TornAPIError("Torn API v1 endpoints are not allowed")
        if path.startswith("/v2"):
            raise TornAPIError("Torn API v2 base URL is already configured; remove '/v2' from paths")
        await self.rate_limiter.acquire()
        await self._ensure_session()
        
        timeout = aiohttp.ClientTimeout(total=60, connect=15, sock_connect=15, sock_read=45)
        backoffs = [0.5, 1.0, 2.0]
        attempts = len(backoffs)

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                async with self.session.get(
                    f"{config.TORN_BASE_URL}{path}",
                    params=params,
                    timeout=timeout,
                ) as resp:
                    elapsed = time.perf_counter() - started
                    log.debug("Torn API attempt=%s method=GET path=%s status=%s elapsed=%.3fs", attempt, path, resp.status, elapsed)

                    if resp.status in (520, 522, 523, 524):
                        raise TornAPIError(f"Torn API is currently unreachable (Cloudflare {resp.status}).")

                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = await resp.text()

                    if resp.status >= 400:
                        raise TornAPIError(f"Torn API error (HTTP {resp.status}).")

                    if isinstance(data, dict) and "error" in data:
                        error = data["error"]
                        msg = str(error.get("error", error)) if isinstance(error, dict) else str(error)
                        if "rate limit" in msg.lower():
                            raise TornAPIRateLimitError(msg)
                        elif "permission" in msg.lower() or "access" in msg.lower():
                            raise TornAPIPermissionError(msg)
                        raise TornAPIError(msg)
                    return data
            except TornAPIRateLimitError:
                raise
            except TornAPIPermissionError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                elapsed = time.perf_counter() - started
                log.debug(
                    "Torn API attempt=%s method=GET path=%s status=exception elapsed=%.3fs error=%s",
                    attempt,
                    path,
                    elapsed,
                    exc,
                )
                if attempt == attempts:
                    if isinstance(exc, asyncio.TimeoutError):
                        log.exception("Torn API request timed out after retries: path=%s", path)
                        raise TornAPIError("Request timed out")
                    log.exception("Torn API request failed after retries: path=%s", path)
                    raise TornAPIError(f"Network error: {exc}")
                await asyncio.sleep(backoffs[attempt - 1])
            except TornAPIError as exc:
                if attempt == attempts:
                    log.exception("Torn API request failed after retries: path=%s", path)
                    if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                        raise TornAPIError("Request timed out")
                    raise
                await asyncio.sleep(backoffs[attempt - 1])

        raise TornAPIError("Request timed out")

    async def validate_api_key(self, api_key: str) -> Tuple[int, int, str, set]:
        """Validate API key by fetching user identity from Torn API v2."""
        user = await self._request("/user", {"selections": "basic,discord", "key": api_key})
        discord_raw = (user.get("discord") or {}).get("discord_id") if isinstance(user, dict) else None
        if discord_raw in (None, "", 0, "0"):
            raise TornAPIError("Torn did not return discord_id; ensure key has 'discord' permission")
        discord_id = int(discord_raw)
        torn_id = int(user["profile"]["id"])
        torn_name = str((user.get("profile") or {}).get("name") or user.get("name") or "").strip()
        return discord_id, torn_id, torn_name, set()

    async def get_user_data(self, api_key: str) -> Dict:
        return await self._request("/user", {"selections": "basic,discord,bars,cooldowns", "key": api_key})
    
    async def get_user_log(self, api_key: str, limit: int = 200) -> List[Dict]:
        data = await self._request("/user", {"selections": "log", "key": api_key})
        if isinstance(data, dict):
            if "log" in data:
                log_data = data["log"]
                if isinstance(log_data, list):
                    return log_data[:limit]
                elif isinstance(log_data, dict):
                    return list(log_data.values())[:limit]
            for v in data.values():
                if isinstance(v, list) and v:
                    return v[:limit]
        return []
    
    async def get_torn_timestamp(self, api_key: str) -> int:
        return await self.get_torn_time()

    async def get_torn_items(self, api_key: str) -> Dict:
        """Fetch Torn item index (names + images) from Torn API v2."""
        return await self._request("/torn", {"selections": "items", "key": api_key})
    
    async def verify_payment(self, api_key: str, recipient_torn_id: int,
                             payment_type: str, amount: int,
                             item_id: Optional[int] = None,
                             since_timestamp: Optional[int] = None) -> Optional[Dict]:
        if payment_type != "item" or item_id is None:
            return None
        return await self.verify_item_payment(
            api_key=api_key,
            recipient_torn_id=recipient_torn_id,
            required_item_id=item_id,
            amount=amount,
            since_timestamp=since_timestamp,
        )
    
    def _matches_payment(self, entry: Dict, recipient_id: int, payment_type: str,
                         amount: int, item_id: Optional[int] = None) -> bool:
        if payment_type != "item" or item_id is None:
            return False

        details_id = (entry.get("details") or {}).get("id")
        if details_id != 4102:
            return False

        data = entry.get("data") or {}
        if int(data.get("receiver") or 0) != int(recipient_id):
            return False

        qty = sum(
            int(it.get("qty") or 0)
            for it in (data.get("items") or [])
            if int(it.get("id") or 0) == int(item_id)
        )
        return qty >= int(amount)

    async def verify_item_payment(self, api_key: str, recipient_torn_id: int,
                                  required_item_id: Optional[int], amount: int,
                                  item_id: Optional[int] = None,
                                  since_timestamp: Optional[int] = None) -> Optional[Dict]:
        if not required_item_id and item_id:
            required_item_id = item_id
        if not required_item_id:
            return None
        try:
            logs = await self.get_user_log(api_key, limit=200)
        except Exception as exc:
            log.warning(
                "Payment verification failed while reading Torn logs recipient=%s required_item_id=%s amount=%s since=%s error=%s",
                recipient_torn_id,
                required_item_id,
                amount,
                since_timestamp,
                type(exc).__name__,
            )
            raise
        for entry in logs:
            timestamp = int(entry.get("timestamp") or 0)
            if since_timestamp and timestamp < since_timestamp:
                continue
            if self._matches_payment(entry, recipient_torn_id, "item", amount, required_item_id):
                return entry
        return None
    
    async def check_overdose(self, api_key: str, since_timestamp: Optional[int] = None) -> Optional[Dict]:
        logs = await self.get_user_log(api_key)
        for entry in logs:
            if since_timestamp and entry.get("timestamp", 0) < since_timestamp:
                continue
            if entry.get("log_type") == config.LOG_IDS["xanax_overdose"]:
                return entry
            s = json.dumps(entry, ensure_ascii=False).lower()
            if "overdose" in s and "xanax" in s:
                return entry
        return None
    
    async def verify_xanax_payment(self, api_key: str, recipient_torn_id: int,
                                    xanax_count: int, since_timestamp: Optional[int] = None) -> Optional[Dict]:
        return await self.verify_item_payment(
            api_key=api_key,
            recipient_torn_id=recipient_torn_id,
            required_item_id=config.XANAX_ITEM_ID,
            amount=xanax_count,
            since_timestamp=since_timestamp,
        )
    
    async def verify_dvd_payment(self, api_key: str, recipient_torn_id: int,
                                  dvd_count: int, since_timestamp: Optional[int] = None) -> Optional[Dict]:
        """Verify Erotic DVD payment."""
        return await self.verify_item_payment(
            api_key=api_key,
            recipient_torn_id=recipient_torn_id,
            required_item_id=config.DVD_ITEM_ID,
            amount=dvd_count,
            since_timestamp=since_timestamp,
        )


    @staticmethod
    def _extract_log_id(entry: Dict) -> str:
        for key in ("id", "log_id", "log", "logid"):
            value = entry.get(key)
            if value not in (None, ""):
                return str(value)
        return "unknown"

    async def verify_host_tax_payment(
        self,
        *,
        api_key: str,
        recipient_torn_id: int,
        tax_type: str,
        item_id: Optional[int] = None,
        quantity: Optional[int] = None,
        cash_amount: Optional[int] = None,
        since_timestamp: Optional[int] = None,
    ) -> Optional[Dict]:
        logs = await self.get_user_log(api_key, limit=200)
        for entry in logs:
            timestamp = int(entry.get("timestamp") or 0)
            if since_timestamp and timestamp < since_timestamp:
                continue

            data = entry.get("data") or {}
            details = entry.get("details") or {}
            receiver = int(data.get("receiver") or 0)
            if receiver != int(recipient_torn_id):
                continue

            if tax_type == "item":
                if int(details.get("id") or 0) != 4102:
                    continue
                wanted_item = int(item_id or 0)
                wanted_qty = int(quantity or 0)
                if wanted_item not in (206, 366) or wanted_qty < 1:
                    continue
                qty = sum(
                    int(it.get("qty") or 0)
                    for it in (data.get("items") or [])
                    if int(it.get("id") or 0) == wanted_item
                )
                if qty == wanted_qty:
                    return entry

            if tax_type == "cash":
                wanted_cash = int(cash_amount or 0)
                if wanted_cash < 1:
                    continue
                sent_cash = int(data.get("money") or data.get("cash") or data.get("amount") or 0)
                if sent_cash == wanted_cash:
                    return entry
        return None
    
    async def get_user_bars(self, api_key: str) -> Dict:
        """Get user's energy, nerve, happy, life bars."""
        data = await self._request("/user", {"selections": "bars", "key": api_key})
        bars = data.get("bars", data.get("bars_info", {}))
        return {
            "energy": bars.get("energy", {}).get("current", 0),
            "energy_max": bars.get("energy", {}).get("maximum", 150),
            "nerve": bars.get("nerve", {}).get("current", 0),
            "happy": bars.get("happy", {}).get("current", 0),
            "life": bars.get("life", {}).get("current", 0)
        }
    
    async def get_drug_cooldown(self, api_key: str) -> int:
        """Get remaining drug cooldown in seconds."""
        data = await self._request("/user", {"selections": "cooldowns", "key": api_key})
        cooldowns = data.get("cooldowns", {})
        return int(cooldowns.get("drug", 0))

    async def get_user_bars_v2(self, api_key: str) -> Dict:
        """Fetch user bars from Torn v2 endpoint."""
        return await self._request("/user/bars", {"key": api_key})

    async def get_user_cooldowns_v2(self, api_key: str) -> Dict:
        """Fetch user cooldowns from Torn v2 endpoint."""
        return await self._request("/user/cooldowns", {"key": api_key})

    async def get_user_log_v2(self, api_key: str, log_id: int, limit: int = 1) -> Dict:
        """Fetch a filtered user log feed from Torn v2 endpoint."""
        return await self._request(
            "/user/log",
            {"log": int(log_id), "limit": int(limit), "key": api_key},
        )
    
    async def get_item_send_receive_logs(self, api_key: str, limit: int = 5) -> List[Dict]:
        data = await self._request("/user/log", {"cat": 85, "limit": limit, "key": api_key})
        log_data = data.get("log") if isinstance(data, dict) else None
        if isinstance(log_data, list):
            return log_data[:limit]
        if isinstance(log_data, dict):
            return list(log_data.values())[:limit]
        return []

    async def get_user_logs(self, api_key: str, limit: int = 5,
                            log_types: Optional[List[int]] = None) -> List[Dict]:
        """Get user logs, optionally filtered by log type."""
        logs = await self.get_item_send_receive_logs(api_key, limit=limit)
        if log_types:
            logs = [
                entry for entry in logs
                if (entry.get("details") or {}).get("id") in log_types
                or entry.get("log_type") in log_types
            ]
        return logs
    
    async def get_torn_time(self) -> int:
        """Get current Torn City timestamp without needing an API key.
        Falls back to system time if no API is available."""
        # Torn time is same as UTC, just return current timestamp
        import time
        return int(time.time())
    
    async def check_drug_use_logs(self, api_key: str, since_timestamp: Optional[int] = None) -> List[Dict]:
        """Check for drug use events in user logs (for insurance monitoring)."""
        logs = await self.get_user_log(api_key, limit=200)
        drug_events = []
        
        # Drug-related log type IDs from Torn API
        drug_log_types = {
            config.LOG_IDS.get("xanax_use", 2290),
            config.LOG_IDS.get("xanax_overdose", 2291),
            config.LOG_IDS.get("ecstasy_use", 2286),
            config.LOG_IDS.get("ecstasy_overdose", 2287),
        }
        
        for entry in logs:
            timestamp = entry.get("timestamp", 0)
            if since_timestamp and timestamp <= since_timestamp:
                continue
            
            log_type = entry.get("log_type") or entry.get("log")
            
            # Check by log type ID
            if log_type in drug_log_types:
                drug_events.append(entry)
                continue
            
            # Fallback: check by text content
            text = json.dumps(entry, ensure_ascii=False).lower()
            if any(drug in text for drug in ["overdose", "xanax", "ecstasy"]):
                if "use" in text or "overdose" in text or "took" in text:
                    drug_events.append(entry)
        
        return drug_events
    
    async def identify_overdose_event(self, log_entry: Dict) -> Optional[Dict]:
        """Identify if a log entry is an overdose event and extract details."""
        log_type = log_entry.get("log_type") or log_entry.get("log")
        text = json.dumps(log_entry, ensure_ascii=False).lower()
        
        # Check for xanax overdose
        if log_type == config.LOG_IDS.get("xanax_overdose", 2291) or \
           ("overdose" in text and "xanax" in text):
            return {
                "type": "xanax_overdose",
                "drug": "xanax",
                "timestamp": log_entry.get("timestamp", 0),
                "log_id": log_entry.get("id") or log_entry.get("log_id"),
                "raw": log_entry
            }
        
        # Check for ecstasy overdose
        if log_type == config.LOG_IDS.get("ecstasy_overdose", 2287) or \
           ("overdose" in text and "ecstasy" in text):
            return {
                "type": "ecstasy_overdose",
                "drug": "ecstasy",
                "timestamp": log_entry.get("timestamp", 0),
                "log_id": log_entry.get("id") or log_entry.get("log_id"),
                "raw": log_entry
            }
        
        return None


_torn_api: Optional[TornAPIClient] = None


def init_torn_api() -> TornAPIClient:
    """Initialize the Torn API client singleton."""
    global _torn_api
    _torn_api = TornAPIClient()
    return _torn_api


def get_torn_api() -> TornAPIClient:
    """Get the Torn API client singleton."""
    if _torn_api is None:
        raise RuntimeError("Torn API not initialized. Call init_torn_api() first.")
    return _torn_api
