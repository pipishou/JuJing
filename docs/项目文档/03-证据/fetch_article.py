import html as htmllib
import pathlib
import re
import urllib.request

URL = "https://mp.weixin.qq.com/s/GFKEw4vjTJZUUR8SCddIKQ"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x18003128) NetType/WIFI Language/zh_CN")

req = urllib.request.Request(URL, headers={"User-Agent": UA, "Referer": "https://mp.weixin.qq.com/"})
raw = urllib.request.urlopen(req, timeout=40).read()
text = None
for enc in ("utf-8", "gb18030"):
    try:
        cand = raw.decode(enc)
        if "js_content" in cand or "rich_media" in cand:
            text = cand
            break
    except UnicodeDecodeError:
        continue
if text is None:
    text = raw.decode("utf-8", "replace")

title = ""
m = re.search(r"var\s+msg_title\s*=\s*['\"](.*?)['\"]", text, re.S)
if m:
    title = htmllib.unescape(m.group(1)).strip()

m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*<div[^>]*class="rich_media_tool', text, re.S)
body = m.group(1) if m else ""
if not body:
    m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*)', text, re.S)
    body = m.group(1)[:400000] if m else text[:400000]

body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
body = re.sub(r"<style.*?</style>", "", body, flags=re.S)
body = re.sub(r"<br\s*/?>", "\n", body)
body = re.sub(r"</p>|</section>|</h\d>|</li>|</div>", "\n", body)
body = re.sub(r"<[^>]+>", "", body)
body = htmllib.unescape(body)
body = re.sub(r"[ \t\u00a0\u3000]+", " ", body)
body = re.sub(r"\n{3,}", "\n\n", body).strip()

out = pathlib.Path(r"D:\work\产品类\个人产品\bot-2\_tmp_article.txt")
out.write_text("TITLE: " + title + "\n\n" + body, encoding="utf-8")
print("TITLE:", title)
print("BODY_CHARS:", len(body))
print("----HEAD----")
print(body[:6000])
