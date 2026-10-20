#!/usr/bin/env python3
"""Free journalist email finder (jaystarz1 fork addition).

Order of evidence, strongest first:
  1. published  - the address appears on the article, the author's page, or another page
                  passed with --url (mailto, text, JSON-LD, Cloudflare-obfuscated), and its
                  local part matches the journalist's name.
  2. smtp       - not published; built from common newsroom patterns (and any pattern seen in
                  other addresses on the outlet's pages) and accepted by the outlet's mail
                  server while a random address is rejected.
  3. inferred   - the pattern guess could not be checked (catch-all server, probe refused).
                  Shown with its pattern, never presented as verified.

Usage:
  find_email.py --name "Jane Doe" --domain theglobeandmail.com --url ARTICLE_URL [--url AUTHOR_URL] [--no-smtp]
Prints one JSON object.
"""
import argparse
import html
import json
import random
import re
import smtplib
import socket
import string
import subprocess
import unicodedata
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
AUTHOR_HREF_RE = re.compile(r"/(author|authors|people|staff|by|profile|profiles|journalist|writers?|reporters?)/", re.I)
PROBE_FROM = "probe@barkerhrs.com"
HELO = "barkerhrs.com"
MAX_AUTHOR_PAGES = 4
MAX_PROBES = 8


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def name_parts(name):
    parts = [norm(p) for p in re.split(r"[\s\-]+", name) if norm(p)]
    if len(parts) < 2:
        raise SystemExit(json.dumps({"name": name, "error": "need a first and last name"}))
    return parts[0], parts[-1]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(2_000_000).decode("utf-8", "replace"), r.geturl()
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def cf_decode(hexstr):
    key = int(hexstr[:2], 16)
    return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))


def extract_emails(page):
    found = set()
    for m in re.finditer(r'data-cfemail="([0-9a-fA-F]+)"', page):
        found.add(cf_decode(m.group(1)))
    for m in re.finditer(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)', page):
        found.add(cf_decode(m.group(1)))
    text = html.unescape(page)
    text = re.sub(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", text, flags=re.I)
    for m in re.finditer(r"mailto:([^\"'?>\s]+)", text, re.I):
        found.add(urllib.parse.unquote(m.group(1)))
    found.update(EMAIL_RE.findall(text))
    clean = set()
    for e in found:
        e = e.strip().strip(".").lower()
        if EMAIL_RE.fullmatch(e) and not re.search(r"\.(png|jpe?g|gif|webp|svg|css|js)$", e):
            clean.add(e)
    return clean


def author_links(page, base, first, last):
    links = []
    for m in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.I | re.S):
        href, label = m.group(1), norm(re.sub(r"<[^>]+>", "", m.group(2)))
        h = norm(href)
        if (first in label and last in label) or (AUTHOR_HREF_RE.search(href) and last in h):
            u = urllib.parse.urljoin(base, href)
            if u.startswith("http") and u not in links:
                links.append(u)
    return links[:MAX_AUTHOR_PAGES]


def matches_name(local, first, last):
    l = norm(local)
    return last in l and (first in l or l.startswith(first[0]) or l.endswith(first[0]))


PATTERNS = [
    ("first.last", lambda f, l: f"{f}.{l}"),
    ("firstlast", lambda f, l: f"{f}{l}"),
    ("flast", lambda f, l: f"{f[0]}{l}"),
    ("first_last", lambda f, l: f"{f}_{l}"),
    ("f.last", lambda f, l: f"{f[0]}.{l}"),
    ("first", lambda f, l: f),
    ("firstl", lambda f, l: f"{f}{l[0]}"),
    ("last", lambda f, l: l),
]


NAME_NEAR_RE = re.compile(r"\b([A-Z][a-z'\-]+)\s+(?:[A-Z]\.\s+)?([A-Z][a-z'\-]+)\b")


def learn_patterns(page_texts, domain):
    """Count which pattern turns a nearby person's name into an address on the domain."""
    counts = {}
    for text in page_texts:
        text = html.unescape(text)
        for m in re.finditer(r"([A-Za-z0-9._%+-]+)@" + re.escape(domain), text, re.I):
            local = m.group(1).lower()
            window = text[max(0, m.start() - 400): m.end() + 400]
            for f, l in NAME_NEAR_RE.findall(window):
                f, l = norm(f), norm(l)
                if not f or not l:
                    continue
                for name, fn in PATTERNS:
                    if fn(f, l) == local:
                        counts[name] = counts.get(name, 0) + 1
                        break
    return counts


def rank_patterns(learned, forced):
    """Forced pattern first, then patterns learned from the outlet, then common newsroom order."""
    order = list(PATTERNS)
    order.sort(key=lambda p: -learned.get(p[0], 0))
    if forced:
        order.sort(key=lambda p: p[0] != forced)
    return order


def mx_hosts(domain):
    try:
        out = subprocess.run(["dig", "+short", "MX", domain], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    rows = sorted((int(p[0]), p[1].rstrip(".")) for p in (l.split() for l in out.splitlines()) if len(p) == 2)
    return [h for _, h in rows]


def smtp_check(domain, locals_):
    """Returns (catch_all, {local: accepted_bool}) or (None, reason) when probing fails."""
    hosts = mx_hosts(domain)
    if not hosts:
        return None, "no MX record"
    last_err = "probe refused"
    for host in hosts[:2]:
        try:
            s = smtplib.SMTP(host, 25, timeout=8)
            s.ehlo(HELO)
            code, _ = s.mail(PROBE_FROM)
            if code >= 400:
                s.quit()
                continue
            junk = "zz" + "".join(random.choices(string.ascii_lowercase + string.digits, k=14))
            catch_all = s.rcpt(f"{junk}@{domain}")[0] in (250, 251)
            results = {}
            for loc in locals_[:MAX_PROBES]:
                results[loc] = s.rcpt(f"{loc}@{domain}")[0] in (250, 251)
            s.quit()
            return catch_all, results
        except (smtplib.SMTPException, OSError, socket.timeout) as e:
            last_err = f"{host}: {type(e).__name__}"
            continue
    return None, last_err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--domain", required=True, help="outlet mail domain, e.g. cbc.ca")
    ap.add_argument("--url", action="append", default=[], help="article, author page, bio, or contact page")
    ap.add_argument("--no-smtp", action="store_true")
    ap.add_argument("--pattern", choices=[p[0] for p in PATTERNS],
                    help="outlet address format found by search, e.g. flast")
    a = ap.parse_args()

    first, last = name_parts(a.name)
    domain = a.domain.lower().removeprefix("www.")
    pages, errors, all_emails, texts = [], {}, {}, []
    queue = list(a.url)
    seen = set()
    while queue:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        page, final = fetch(u)
        if page is None:
            errors[u] = final
            continue
        pages.append(final)
        texts.append(page)
        for e in extract_emails(page):
            all_emails.setdefault(e, final)
        if len(seen) <= len(a.url):
            queue.extend(l for l in author_links(page, final, first, last) if l not in seen)

    result = {"name": a.name, "domain": domain, "pages_read": pages, "fetch_errors": errors}

    published = [(e, src) for e, src in all_emails.items() if matches_name(e.split("@")[0], first, last)]
    if published:
        published.sort(key=lambda x: (not x[0].endswith("@" + domain), len(x[0])))
        e, src = published[0]
        result.update(email=e, method="published", confidence="high", source=src,
                      other_published=[p for p, _ in published[1:]])
        print(json.dumps(result, indent=2))
        return

    samples = [e for e in all_emails if e.endswith("@" + domain)]
    learned = learn_patterns(texts, domain)
    ranked = rank_patterns(learned, a.pattern)
    candidates = [(name, fn(first, last)) for name, fn in ranked]
    result["domain_samples"] = samples[:10]
    result["patterns_learned"] = learned

    if a.no_smtp:
        name, loc = candidates[0]
        result.update(email=f"{loc}@{domain}", method="inferred", confidence="low", pattern=name,
                      note="not published; SMTP check skipped")
        print(json.dumps(result, indent=2))
        return

    catch_all, checks = smtp_check(domain, [loc for _, loc in candidates])
    basis = ("given by --pattern" if a.pattern else
             "learned from the outlet's pages" if learned else "common newsroom default")
    result["pattern_basis"] = basis
    guess_conf = "medium-low" if (a.pattern or learned) else "low"
    if catch_all is None:
        name, loc = candidates[0]
        result.update(email=f"{loc}@{domain}", method="inferred", confidence=guess_conf, pattern=name,
                      note=f"not published; SMTP check unavailable ({checks})")
    elif catch_all:
        name, loc = candidates[0]
        result.update(email=f"{loc}@{domain}", method="inferred", confidence=guess_conf, pattern=name,
                      note="not published; mail server accepts any address (catch-all), so unverifiable")
    else:
        hit = next(((n, l) for n, l in candidates if checks.get(l)), None)
        if hit:
            result.update(email=f"{hit[1]}@{domain}", method="smtp", confidence="medium", pattern=hit[0],
                          note="not published; mail server accepted it and rejected a random address")
        else:
            result.update(email=None, method="none", confidence="none",
                          note="not published; server rejected every common pattern")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
