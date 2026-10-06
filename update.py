#!/usr/bin/env python3
"""
TVBox 聚合源自动更新（终极净化与全速优化版）
特性：
  1. 点播站点（sites）与电视直播（lives）全面成人/色情过滤
  2. 修复直播源（如“18资源丰富”）、爬虫源（“黄人秒播”）等隐蔽漏网之鱼
  3. 多线程极速并发测速（40秒~1分钟内完成）
  4. 产物全面按连接速度/延迟严格排序
产物对应：
  - t1.json: 精选健康极速版（前10名，无色情，秒开秒播）
  - t2.json: 全量健康版（彻底净化点播+直播，按速度排序）
  - t3.json: 多仓版（按连接延迟升序）
  - t4.json: 成人独立专属版（隔离出的色情点播+18禁电视直播）
"""
import json, sys, re, subprocess, os, time
import urllib.parse
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
CF_PROXY = os.environ.get("CF_PROXY", "")

# ── 终极拦截词库：覆盖点播暗号、电视直播暗号、拼音缩写 ──
ADULT_KEYWORDS = [
    # 显性词汇
    "成人", "伦理", "福利", "情色", "色情", "三级", "无码", "有码", 
    "番号", "女优", "自拍", "偷拍", "乱伦", "激情", "色色", "黄片", 
    "性爱", "春水", "色猫", "AV", "jav", "hentai", "pornhub", "xvideos", 
    "r18", "18禁", "18+", "18资源", "撸班", "高潮", "幼女", "巨乳", 
    "熟女", "痴女", "黄色仓库", "草榴", "性奴", "调教", "丝袜", "美腿", "诱惑",
    
    # 电视直播特有成人词 & 擦边词
    "午夜", "深夜", "福利台", "成人台", "黄人", "小黄人",

    # 拼音缩写黑名单 (黄色仓库 hsck, 草榴 clun 等)
    "hsck", "caoliu", "clun", "t66y", "sezy", "avzy", "luzy", 
    "fulizy", "slzy", "mdzy", "91zy", "tmzy", "ckzy",
    
    # 厂牌与暗号
    "麻豆", "探花", "91", "天美", "蜜桃", "精东", "糖心", "星空", 
    "乐播", "老司机", "1024", "秋葵", "香蕉", "茄子", "微拍", 
    "暗网", "福利姬", "jable", "missav", "hanime", "rouvideo",
    
    # 英文特征
    "adult", "sex", "xxx", "fuli", "madou"
]

def is_adult(item):
    """深度全字段扫描：支持点播站字典、直播源字典、字符串"""
    if isinstance(item, dict):
        text_parts = [
            str(item.get('name', '')),
            str(item.get('key', '')),
            str(item.get('api', '')),
            str(item.get('url', '')),          # 关键：检查直播源链接
            str(item.get('ext', '')),          # 关键：检查爬虫参数
            str(item.get('categories', ''))
        ]
        text = " ".join(text_parts).lower()
    else:
        text = str(item).lower()

    # 1. 关键词、拼音缩写与暗号排查
    for kw in ADULT_KEYWORDS:
        if kw.lower() in text:
            return True

    # 2. 正则阻击
    if re.search(r'(?i)(?:\b|_|-)(av|adult|sex|xxx|r18|fuli|18\+|porno|cl)(?:\b|_|-|\.|\d)', text):
        return True

    return False

def curl(url, timeout=3, max_time=5, via_proxy=False):
    actual_url = f"{CF_PROXY}?u={urllib.parse.quote(url, safe='')}" if (via_proxy and CF_PROXY) else url
    try:
        r = subprocess.run(["curl", "-s", "-L", "--connect-timeout", str(timeout),
                           "--max-time", str(max_time), "-A", "Mozilla/5.0", actual_url],
                          capture_output=True, timeout=max_time + 2)
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
    name, url = item
    try:
        t0 = time.time()
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                           "--connect-timeout", "3", "--max-time", "4",
                           "-L", "-A", "Mozilla/5.0", url],
                          capture_output=True, timeout=5)
        code = r.stdout.decode().strip()
        lat = int((time.time() - t0) * 1000) if code.startswith(("2", "3")) else 99999
    except Exception:
        lat = 99999
    return (name, url, lat)

def test_play_speed(api, stype):
    base = re.sub(r'[?&]ac=list.*', '', api.rstrip("/"))
    body = curl(build_url(base, "ac=list"), timeout=3, max_time=4)
    if not body or len(body) < 50: return 0, 0, "失败"

    vids = []
    if stype == 0:
        vids = re.findall(r'<id>(\d+)</id>', body)[:1]
    else:
        try:
            j = json.loads(body, strict=False)
            vids = [str(v["vod_id"]) for v in (j.get("list") or [])[:1]]
        except Exception:
            return 0, 0, "失败"
    if not vids: return 0, 0, "失败"

    vid = vids[0]
    detail = curl(build_url(base, f"ac=detail&ids={vid}"), timeout=3, max_time=4)
    if not detail: return 0, 0, "失败"

    m3u8s = []
    if stype == 0:
        m3u8s = extract_m3u8(detail)
    else:
        try:
            dj = json.loads(detail, strict=False)
            for v in (dj.get("list") or []):
                m3u8s.extend(extract_m3u8(v.get("vod_play_url", "")))
        except Exception:
            pass
    if not m3u8s: return 0, 0, "失败"

    play = m3u8s[0]
    t0 = time.time()
    master = curl(play, timeout=3, max_time=4)
    ttfb = int((time.time() - t0) * 1000)
    if not master: return 0, 0, "失败"

    media_url = None
    if "#EXT-X-STREAM-INF" in master:
        for i, line in enumerate(master.strip().split("\n")):
            if "STREAM-INF" in line:
                lines = master.strip().split("\n")
                sub = lines[i+1].strip() if i+1 < len(lines) else ""
                if sub and not sub.startswith("#"):
                    media_url = resolve_url(play, sub); break
    elif "#EXTINF" in master:
        media_url = play
    if not media_url: return 0, 0, "失败"

    media = curl(media_url, timeout=3, max_time=4)
    if "#EXTINF" not in media: return 0, 0, "失败"
    segs = get_segments(media, media_url)
    if not segs: return 0, 0, "失败"

    tb, tt, ok = 0, 0, 0
    for s in segs[:2]:
        r = subprocess.run(["curl", "-s", "-o", "/dev/null",
                           "-w", "%{http_code},%{size_download},%{time_total}",
                           "--connect-timeout", "3", "--max-time", "4", s],
                          capture_output=True, timeout=5)
        parts = r.stdout.decode().strip().split(",")
        code = parts[0] if parts else "000"
        sz = int(float(parts[1])) if len(parts) > 1 and parts[1] else 0
        dl = float(parts[2]) if len(parts) > 2 and parts[2] else 99
        if code.startswith("2") and sz > 1000:
            tb += sz; tt += dl; ok += 1
            break

    if ok >= 1 and tt > 0:
        return ttfb, int((tb / 1024) / tt), "OK"
    return 0, 0, "失败"

def probe_task(item):
    api, (src_name, stype) = item
    ttfb, speed, st = test_play_speed(api, stype)
    if st == "OK":
        return (ttfb, speed, api, stype)
    return None

def main():
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] ⚡ 开始更新（点播+直播双重净化，按速度排序）...")

    # 1. 抓取源列表
    html = curl("https://tvbox.clbug.com/user.php", timeout=5, max_time=8)
    src_urls = re.findall(r'data-url="([^"]+)"', html)
    src_names = re.findall(r'<td class="td-name">([^<]+)</td>', html)
    sources = [(n.strip(), u.strip().replace("&amp;", "&"))
               for n, u in zip(src_names, src_urls)
               if u.strip() and not u.strip().startswith("#")]

    # 2. 25线程并发测源延迟
    available = []
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(test_source_latency, s) for s in sources]
        for f in as_completed(futures):
            res = f.result()
            if res[2] < 99999: available.append(res)
            
    available.sort(key=lambda x: x[2])
    print(f"  可用源数量: {len(available)} 个")

    # 3. 合并站点与全面成人分流（点播 + 直播）
    all_sites, adult_sites = [], []
    all_lives, adult_lives = [], []  # 直播源也进行健康与成人隔离
    all_parses = []
    site_keys, live_keys, parse_keys = set(), set(), set()
    spider_jars = {}
    collect_sources = {}

    for name, url, lat in available:
        data = parse_json(curl(url, timeout=3, max_time=5))
        if not data: continue

        spider = data.get("spider", "")
        if spider:
            abs_spider = resolve_spider(spider, url)
            spider_jars[abs_spider] = spider_jars.get(abs_spider, 0) + 1

        # ── 处理点播站点（sites） ──
        for s in (data.get("sites") or []):
            key = s.get("key", "")
            if not key or key in site_keys: continue
            site_keys.add(key)
            s["name"] = f"[{lat}ms|{name}] {s.get('name', key)}"
            s["_lat"] = lat

            if is_adult(s):
                adult_sites.append(s)
            else:
                all_sites.append(s)
                st = s.get("type", -1)
                api = s.get("api", "")
                if st in (0, 1) and api.startswith("http") and api not in collect_sources:
                    collect_sources[api] = (name, st)

        # ── 处理电视直播（lives） ──
        for l in (data.get("lives") or []):
            u = l.get("url", "")
            if not u or u in live_keys: continue
            live_keys.add(u)

            if is_adult(l):
                adult_lives.append(l)  # 成人直播隔离到 t4
            else:
                all_lives.append(l)    # 健康直播保留在 t2

        for p in (data.get("parses") or []):
            u = p.get("url", "")
            if u and u not in parse_keys: parse_keys.add(u); all_parses.append(p)

    print(f"  点播统计: 健康 {len(all_sites)} 个 | 成人隔离 {len(adult_sites)} 个")
    print(f"  直播统计: 健康 {len(all_lives)} 个 | 成人隔离 {len(adult_lives)} 个")

    # 4. 真实播放测速（只测健康站）
    PINNED_APIS = ["suoniapi.com", "360zy.com"]
    target_items = []
    for item in collect_sources.items():
        if any(kw in item[0] for kw in PINNED_APIS):
            target_items.insert(0, item)
        else:
            target_items.append(item)
    target_items = target_items[:50]

    collect_results = []
    with ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(probe_task, item) for item in target_items]
        for f in as_completed(futures):
            res = f.result()
            if res: collect_results.append(res)

    collect_results.sort(key=lambda x: (-x[1], x[0]))

    # 把测速结果回填到 all_sites 中用于排序
    speed_map = {api: (ttfb, speed) for ttfb, speed, api, _ in collect_results}
    for s in all_sites:
        api = s.get("api", "")
        if api in speed_map:
            s["_speed"] = speed_map[api][1]
            s["_ttfb"] = speed_map[api][0]
        else:
            s["_speed"] = 0
            s["_ttfb"] = 99999

    # ── t2.json 全量健康版按速度排序 ──
    all_sites.sort(key=lambda s: (
        0 if s.get("_speed", 0) > 0 else 1,
        -s.get("_speed", 0),
        s.get("_ttfb", 99999),
        s.get("_lat", 99999)
    ))

    # ── t4.json 成人版按源延迟升序 ──
    adult_sites.sort(key=lambda s: s.get("_lat", 99999))

    best_spider = max(spider_jars, key=spider_jars.get) if spider_jars else ""

    # 清理内部临时字段
    for s in all_sites + adult_sites:
        s.pop("_lat", None)
        s.pop("_speed", None)
        s.pop("_ttfb", None)

    # ── 5. 输出 t2.json (全量纯净健康版，含纯净直播) ──
    full_json = {"spider": best_spider, "sites": all_sites, "lives": all_lives, "parses": all_parses}
    with open(os.path.join(WORK_DIR, "t2.json"), "w", encoding="utf-8") as f:
        json.dump(full_json, f, ensure_ascii=False, indent=2)

    # ── 6. 输出 t4.json (色情独立专属版，含成人点播与成人直播) ──
    adult_json = {"spider": best_spider, "sites": adult_sites, "lives": adult_lives, "parses": all_parses}
    with open(os.path.join(WORK_DIR, "t4.json"), "w", encoding="utf-8") as f:
        json.dump(adult_json, f, ensure_ascii=False, indent=2)

    # ── 7. 输出 t3.json (多仓版) ──
    multi = {"storeHouse": [{"sourceName": f"[{lat}ms] {name}", "sourceUrl": url}
                            for name, url, lat in available]}
    with open(os.path.join(WORK_DIR, "t3.json"), "w", encoding="utf-8") as f:
        json.dump(multi, f, ensure_ascii=False, indent=2)

    # ── 8. 输出 t1.json (精选健康极速版前10) ──
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

    # 9. 输出 sources.txt
    with open(os.path.join(WORK_DIR, "sources.txt"), "w", encoding="utf-8") as f:
        f.write(f"# {ts}\n\n")
        for name, url, lat in available:
            f.write(f"[{lat}ms] {name}\n{url}\n\n")

    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚡ 更新成功！客厅源已彻底净化，色情内容已隔离至 t4.json！")
    return 0

if __name__ == "__main__":
    sys.exit(main())
