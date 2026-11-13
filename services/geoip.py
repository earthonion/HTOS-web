import asyncio
import ipaddress
import json
import time
import urllib.parse
import urllib.request

_CACHE = {}
_CACHE_TTL = 86400


def country_flag(country_code):
    code = (country_code or "").upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in code)


def lookup_ip(ip):
    now = time.time()
    cached = _CACHE.get(ip)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    result = {"flag": "", "location": "", "country_code": ""}
    try:
        address = ipaddress.ip_address(ip)
        if not address.is_global:
            return result
        url = f"https://ipwho.is/{urllib.parse.quote(ip, safe='')}"
        with urllib.request.urlopen(url, timeout=4) as response:
            data = json.load(response)
        if data.get("success") is False:
            return result
        country_code = (data.get("country_code") or "").upper()
        location = ", ".join(
            part for part in (data.get("city"), data.get("region"), data.get("country")) if part
        )
        result = {
            "flag": country_flag(country_code),
            "location": location,
            "country_code": country_code,
        }
    except (ValueError, OSError, json.JSONDecodeError):
        pass
    _CACHE[ip] = (now, result)
    return result


async def enrich_jobs(jobs):
    unique_ips = {
        job.get("user_ip")
        for job in jobs
        if job.get("user_ip") and job["user_ip"] != "unknown"
    }
    locations = {}
    for ip in unique_ips:
        locations[ip] = await asyncio.to_thread(lookup_ip, ip)
    for job in jobs:
        location = locations.get(job.get("user_ip"), {})
        job["user_flag"] = location.get("flag", "")
        job["user_location"] = location.get("location", "")
    return jobs
