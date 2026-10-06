#!/usr/bin/env python3
"""
TVBox 聚合源自动更新（多线程并发优化版）
"""
import json, sys, re, subprocess, os, time
import urllib.parse
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
CF_PROXY = os.environ.get("CF_PROXY", "")  # Cloudflare Worker 代理地址

def curl(url, timeout=10, via_proxy=False):
    actual_url = f"{CF_PROXY}?u={urllib.parse.quote(url, safe='')}" if (via_proxy and CF_PROXY) else url
    try:
        r = subprocess.run(["curl", "-s", "-L", "--connect-timeout", str(timeout),
                           "--max-time", str(timeout * 2), "-A", "Mozilla/5.0", actual_url],
                          capture_output=True, timeout=timeout * 2 + 5)
        return r.stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""

def parse_json(raw):
    raw = raw.lstrip('\ufeff')
    raw = re.sub(r',(\s*[}\]])', r'\1', raw)
    try:
        return json.loads(raw, strict=False)
    except Exception:
        s, e = raw.find('{'), raw.rfind('}')
        if s >= 0 and e > s:
            try:
                return json.loads(raw[s:e+1], strict=False)
            except Exception:
                pass
    return None

def resolve_spider(spider, source_url):
    if not spider: return ""
    if spider.startswith("http"): return spider
    if spider.startswith("./"):
        p = urlparse(source_url)
        return f"{p.scheme}://{p.netloc}{spider[1:]}"
    return spider

def resolve_url(base, path):
    if path.startswith("http"): return path
    if path.startswith("/"): return f"{urlparse(base).scheme}://{urlparse(base).netloc}{path}"
    return urljoin(base, path)

def extract_m3u8(t):
    # 修复：支持匹配带参数的 M3U8（如 .m3u8?token=xxx）
    return re.findall(r'(https?://[^\s"\'<>#\$]+?\.m3u8(?:\?[^\s"\'<>#\$]*)?)', t)

def get_segments(media, media_url):
    urls = []
    lines = media.strip().split("\n")
    for i, line in enumerate(lines):
        if line.startswith("#EXTINF") and i + 1 < len(lines):
            nxt = lines[i+1].strip()
            if nxt and not nxt.startswith("#"):
                urls.append(resolve_url(media_url, nxt))
    return urls

def build_url(base, params):
    return base.rstrip("/") + ("&" if "?" in base else "?") + params

def test_source_latency(item):
    """测试单个源的延迟"""
    name, url = item
    try:
        t0 = time.time()
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                           "--connect-timeout", "5", "--max-time", "10",
                           "-L", "-A", "Mozilla/5.0", url],
                          capture_output=True, timeout=12)
        code = r.stdout.decode().strip()
        lat = int((time.time() - t0) * 1000) if code.startswith(("2", "3")) else 99999
    except Exception:
        lat = 99999
    return (name, url, lat)

def test_play_speed(api, stype, use_proxy=False):
    """真实切片测速"""
    base = re.sub(r'[?&]ac=list.*', '', api.rstrip("/"))
    body = curl(build_url(base, "ac=list"), 10, via_proxy=use_proxy)
    if not body or len(body) < 50: return 0, 0, "列表失败"

    vids = []
    if stype == 0:
        vids = re.findall(r'<id>(\d+)</id>', body)[:2]
    else:
        try:
            j = json.loads(body, strict=False)
            vids = [str(v["vod_id"]) for v in (j.get("list") or [])[:2]]
        except Exception:
            return 0, 0, "解析失败"
    if not vids: return 0, 0, "无ID"

    for vid in vids:
        detail = curl(build_url(base, f"ac=detail&ids={vid}"), 10, via_proxy=use_proxy)
        if not detail: continue
        m3u8s = []
        if stype == 0:
            m3u8s = extract_m3u8(detail)
        else:
            try:
                dj = json.loads(detail, strict=False)
                for v in (dj.get("list") or []):
                    m3u8s.extend(extract_m3u8(v.get("vod_play_url", "")))
            except Exception:
                continue
        if not m3u8s: continue

        for play in m3u8s[:2]:
            t0 = time.time()
            master = curl(play, 10, via_proxy=use_proxy)
            ttfb = int((time.time() - t0) * 1000)
            if not master: continue

            media_url = None
            if "#EXT-X-STREAM-INF" in master:
                for i, line in enumerate(master.strip().split("\n")):
                    if "STREAM-INF" in line:
                        sub = master.strip().split("\n")[i+1].strip() if i+1 < len(master.strip().split("\n")) else ""
                        if sub and not sub.startswith("#"):
                            media_url = resolve_url(play, sub); break
            elif "#EXTINF" in master:
                media_url = play
            if not media_url: continue

            t1 = time.time()
            media = curl(media_url, 10, via_proxy=use_proxy)
            mms = int((time.time() - t1) * 1000)
            if "#EXTINF" not in media: continue
            segs = get_segments(media, media_url)
            if not segs: continue

            tb, tt, ok = 0, 0, 0
            for s in segs[:5]:
                if ok >= 2: break
                seg_url = f"{CF_PROXY}?u={urllib.parse.quote(s, safe='')}" if (use_proxy and CF_PROXY) else s
                r = subprocess.run(["curl", "-s", "-o", "/dev/null",
                                   "-w", "%{http_code},%{size_download},%{time_total}",
                                   "--connect-timeout", "6", "--max-time", "12", seg_url],
                                  capture_output=True, timeout=15)
                parts = r.stdout.decode().strip().split(",")
                code = parts[0] if parts else "000"
                sz = int(float(parts[1])) if len(parts) > 1 and parts[1] else 0
                dl = float(parts[2]) if len(parts) > 2 and parts[2] else 99
                if code.startswith("2") and sz > 1000:
                    tb += sz; tt += dl; ok += 1
            if ok >= 2:
                speed = int((tb / 1024) / tt) if tt > 0 else 0
                return ttfb + mms, speed, "OK"
    return 0, 0, "全部失败"

def probe_single_station(item):
    """单个采集站探活与重试"""
    api, (src_name, stype) = item
    for attempt in range(2):  # 优化：重试降为2次，缩短无效等待
        use_proxy = (attempt == 1 and CF_PROXY)
        ttfb, speed, st = test_play_speed(api, stype, use_proxy=use_proxy)
        if st == "OK":
            return (ttfb, speed, api, stype)
        if attempt < 1:
            time.sleep(1)
    return None

def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] 开始更新...")

    # 1. 获取源列表
    html = curl("https://tvbox.clbug.com/user.php", 20)
    src_urls = re.findall(r'data-url="([^"]+)"', html)
    src_names = re.findall(r'<td class="td-name">([^<]+)</td>', html)
    sources = [(n.strip(), u.strip().replace("&amp;", "&"))
               for n, u in zip(src_names, src_urls)
               if u.strip() and not u.strip().startswith("#")]
    print(f"  源列表获取成功: {len(sources)} 个")

    # 2. 多线程并发测源延迟
    print("  开始并发测试源延迟...")
    available = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(test_source_latency, s) for s in sources]
        for f in as_completed(futures):
            res = f.result()
            if res[2] < 99999:
                available.append(res)
    available.sort(key=lambda x: x[2])
    print(f"  可用源数量: {len(available)}")

    # 3. 抓取并合并站点
    all_sites, all_lives, all_parses = [], [], []
    site_keys, live_keys, parse_keys = set(), set(), set()
    spider_jars = {}
    collect_sources = {}

    for name, url, lat in available:
        data = parse_json(curl(url, 10))
        if not data: continue

        spider = data.get("spider", "")
        if spider:
            abs_spider = resolve_spider(spider, url)
            spider_jars[abs_spider] = spider_jars.get(abs_spider, 0) + 1

        for s in (data.get("sites") or []):
            key = s.get("key", "")
            if not key or key in site_keys: continue
            site_keys.add(key)
            s["name"] = f"[{lat}ms|{name}] {s.get('name', key)}"
            s["_lat"] = lat
            all_sites.append(s)
            
            st = s.get("type", -1)
            api = s.get("api", "")
            if st in (0, 1) and api.startswith("http") and api not in collect_sources:
                collect_sources[api] = (name, st)

        for l in (data.get("lives") or []):
            u = l.get("url", "")
            if u and u not in live_keys: live_keys.add(u); all_lives.append(l)
        for p in (data.get("parses") or []):
            u = p.get("url", "")
            if u and u not in parse_keys: parse_keys.add(u); all_parses.append(p)

    # 4. 多线程并发进行播放测速
    print(f"  并发测速: 测 {len(collect_sources)} 个采集站...")
    collect_results = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(probe_single_station, item) for item in collect_sources.items()]
        for f in as_completed(futures):
            res = f.result()
            if res:
                collect_results.append(res)
                sys.stdout.write(f"\r  已测出 {len(collect_results)} 个高速可用站")
                sys.stdout.flush()
    print()

    collect_results.sort(key=lambda x: (-x[1], x[0]))

    # 置顶指定优质源
    PINNED_APIS = ["suoniapi.com", "360zy.com"]
    pinned = [[] for _ in PINNED_APIS]
    rest = []
    for item in collect_results:
        api = item[2]
        placed = False
        for i, kw in enumerate(PINNED_APIS):
            if kw in api:
                pinned[i].append(item); placed = True; break
        if not placed:
            rest.append(item)
    collect_results = [x for group in pinned for x in group] + rest

    # 标记与全量版排序
    speed_map = {api: (ttfb, speed) for ttfb, speed, api, _ in collect_results}
    for s in all_sites:
        api = s.get("api", "")
        if api in speed_map:
            s["_speed"] = speed_map[api][1]
            s["_speed_ttfb"] = speed_map[api][0]

    all_sites.sort(key=lambda s: (0, -s.get("_speed", 0), s.get("_speed_ttfb", 99999), s.get("_lat", 99999))
                   if s.get("type", -1) in (0, 1) else (1, 0, 0, s.get("_lat", 99999)))

    pinned_sites = [[] for _ in PINNED_APIS]
    other_collect, other_sites = [], []
    for s in all_sites:
        if s.get("type") not in (0, 1):
            other_sites.append(s); continue
        api = s.get("api", "")
        placed = False
        for i, kw in enumerate(PINNED_APIS):
            if kw in api:
                pinned_sites[i].append(s); placed = True; break
        if not placed:
            other_collect.append(s)

    all_sites = [x for group in pinned_sites for x in group] + other_collect + other_sites
    for s in all_sites:
        s.pop("_lat", None); s.pop("_speed", None); s.pop("_speed_ttfb", None)

    # 5. 生成 tvbox_full.json
    best_spider = max(spider_jars, key=spider_jars.get) if spider_jars else ""
    full_json = {"spider": best_spider, "sites": all_sites, "lives": all_lives, "parses": all_parses}
    with open(os.path.join(WORK_DIR, "t2.json"), "w", encoding="utf-8") as f:
        json.dump(full_json, f, ensure_ascii=False, indent=2)

    # 6. 生成 tvbox_multi.json
    pinned_repos = {collect_sources[api][0] for api in collect_sources for kw in PINNED_APIS if kw in api}
    pinned_avail = [x for x in available if x[0] in pinned_repos]
    other_avail = [x for x in available if x[0] not in pinned_repos]
    multi = {"storeHouse": [{"sourceName": f"[{lat}ms] {name}", "sourceUrl": url}
                            for name, url, lat in pinned_avail + other_avail]}
    with open(os.path.join(WORK_DIR, "t3.json"), "w", encoding="utf-8") as f:
        json.dump(multi, f, ensure_ascii=False, indent=2)

    # 7. 生成 tvbox.json (简洁版)
    SIMPLE_LIMIT = 10
    collect_sites = []
    for ttfb, speed, api, stype in collect_results[:SIMPLE_LIMIT]:
        clean_name = urlparse(api).netloc or api.split("/")[2]
        for s in all_sites:
            if s.get("api") == api:
                clean_name = re.sub(r'^\[.*?\]\s*', '', s.get("name", clean_name))
                break
        stable = "稳" if speed > 500 else "中" if speed > 100 else "慢"
        collect_sites.append({
            "key": clean_name,
            "name": f"[{speed}KB/s|{ttfb}ms|{stable}] {clean_name}",
            "type": stype, "api": api,
            "searchable": 1, "quickSearch": 1, "filterable": 0
        })

    with open(os.path.join(WORK_DIR, "t1.json"), "w", encoding="utf-8") as f:
        json.dump({"spider": "", "sites": collect_sites, "lives": [], "parses": []}, f, ensure_ascii=False, indent=2)

    # 8. 保存 sources.txt
    with open(os.path.join(WORK_DIR, "sources.txt"), "w", encoding="utf-8") as f:
        f.write(f"# {ts}\n\n")
        for name, url, lat in available:
            f.write(f"[{lat}ms] {name}\n{url}\n\n")

    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 成功生成所有源！")
    return 0

if __name__ == "__main__":
    sys.exit(main())
