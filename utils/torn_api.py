"""Torn API utilities with rate limiting."""

import aiohttp
import asyncio
import time
import json
import logging
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
            self.session = aiohttp.ClientSession()
    
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
        
        try:
            async with self.session.get(
                f"{config.TORN_BASE_URL}{path}",
                params=params,
                timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                data = await resp.json()
                if isinstance(data, dict) and "error" in data:
                    error = data["error"]
                    msg = str(error.get("error", error)) if isinstance(error, dict) else str(error)
                    if "rate limit" in msg.lower():
                        raise TornAPIRateLimitError(msg)
                    elif "permission" in msg.lower() or "access" in msg.lower():
                        raise TornAPIPermissionError(msg)
                    raise TornAPIError(msg)
                return data
        except aiohttp.ClientError as e:
            raise TornAPIError(f"Network error: {e}")
        except asyncio.TimeoutError:
            raise TornAPIError("Request timed out")

    async def _request_key_info(self, params: Dict) -> Dict:
        await self.rate_limiter.acquire()
        await self._ensure_session()

        try:
            async with self.session.get(
                "https://api.torn.com/key",
                params=params,
                timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                data = await resp.json()
                if isinstance(data, dict) and "error" in data:
                    error = data["error"]
                    msg = str(error.get("error", error)) if isinstance(error, dict) else str(error)
                    if "rate limit" in msg.lower():
                        raise TornAPIRateLimitError(msg)
                    elif "permission" in msg.lower() or "access" in msg.lower():
                        raise TornAPIPermissionError(msg)
                    raise TornAPIError(msg)
                return data
        except aiohttp.ClientError as e:
            raise TornAPIError(f"Network error: {e}")
        except asyncio.TimeoutError:
            raise TornAPIError("Request timed out")
    
    async def validate_api_key(self, api_key: str) -> Tuple[int, int, set]:
        # Key verification is v1-only; keep it separate from v2 requests.
        info = await self._request_key_info({"selections": "info", "key": api_key})
        key_info = info.get("key", info) if isinstance(info, dict) else info
        perms = self._extract_permissions(key_info)
        if not config.REQUIRED_PERMISSIONS.issubset(perms):
            missing = config.REQUIRED_PERMISSIONS - perms
            raise TornAPIPermissionError(f"Missing permissions: {', '.join(missing)}")
        
        user = await self._request("/user", {"selections": "basic,discord", "key": api_key})
        discord_id = int(user["discord"]["discord_id"])
        torn_id = int(user["profile"]["id"])
        return discord_id, torn_id, perms

    def _extract_permissions(self, key_info: Dict) -> set:
        selections = key_info.get("selections") if isinstance(key_info, dict) else None
        perms: set = set()

        if isinstance(selections, dict):
            user_selections = selections.get("user")
            if isinstance(user_selections, list):
                perms.update(user_selections)
            elif isinstance(user_selections, str):
                perms.update({s.strip() for s in user_selections.split(",") if s.strip()})
            elif isinstance(user_selections, dict):
                for field in ("selections", "permissions", "allowed"):
                    values = user_selections.get(field)
                    if isinstance(values, list):
                        perms.update(values)
                        break
                    if isinstance(values, str):
                        perms.update({s.strip() for s in values.split(",") if s.strip()})
                        break
        elif isinstance(selections, list):
            perms.update(selections)
        elif isinstance(selections, str):
            perms.update({s.strip() for s in selections.split(",") if s.strip()})

        access_type = str(key_info.get("access_type", "")).lower() if isinstance(key_info, dict) else ""
        access_level = key_info.get("access_level") if isinstance(key_info, dict) else None
        if not perms and (access_type == "full" or (isinstance(access_level, int) and access_level >= 3)):
            perms = set(config.REQUIRED_PERMISSIONS)

        return perms
    
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
    
    async def verify_payment(self, api_key: str, recipient_torn_id: int,
                             payment_type: str, amount: int,
                             item_id: Optional[int] = None,
                             since_timestamp: Optional[int] = None) -> Optional[Dict]:
        logs = await self.get_user_log(api_key)
        for entry in logs:
            if since_timestamp and entry.get("timestamp", 0) < since_timestamp:
                continue
            if self._matches_payment(entry, recipient_torn_id, payment_type, amount, item_id):
                return entry
        return None
    
    def _matches_payment(self, entry: Dict, recipient_id: int, payment_type: str,
                         amount: int, item_id: Optional[int] = None) -> bool:
        s = json.dumps(entry, ensure_ascii=False).lower()
        if str(recipient_id) not in s or str(amount) not in s:
            return False
        if payment_type == "cash":
            return any(k in s for k in ["$", "cash", "money", "sent", "transfer"])
        elif payment_type == "item":
            if item_id is None:
                return False
            if str(item_id) not in s:
                if item_id == config.DVD_ITEM_ID and "dvd" not in s:
                    return False
                if item_id == config.XANAX_ITEM_ID and "xanax" not in s:
                    return False
            return True
        return False
    
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
        return await self.verify_payment(
            api_key, recipient_torn_id, "item", xanax_count,
            config.XANAX_ITEM_ID, since_timestamp
        )
    
    async def verify_dvd_payment(self, api_key: str, recipient_torn_id: int,
                                  dvd_count: int, since_timestamp: Optional[int] = None) -> Optional[Dict]:
        """Verify Erotic DVD payment."""
        return await self.verify_payment(
            api_key, recipient_torn_id, "item", dvd_count,
            config.DVD_ITEM_ID, since_timestamp
        )
    
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
    
    async def get_user_logs(self, api_key: str, limit: int = 100, 
                            log_types: Optional[List[int]] = None) -> List[Dict]:
        """Get user logs, optionally filtered by log type."""
        logs = await self.get_user_log(api_key, limit)
        if log_types:
            logs = [l for l in logs if l.get("log_type") in log_types or l.get("log") in log_types]
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

def init_torn_api() -> TornAPIClient:
    global torn_api
    torn_api = TornAPIClient()
    return torn_api

def get_torn_api() -> TornAPIClient:
    if torn_api is None:
        raise RuntimeError("Torn API not initialized")
    return torn_api
