"""Downloads the Bible texts used for scripture matching into the "bible" folder (run once, needs internet):

    python fetch_bible.py

  - Telugu IRV (Indian Revised Version 2019), (c) 2017, 2019 Bridge Connectivity Solutions,
    licensed CC BY-SA 4.0 - source: https://ebible.org/tel2017/   (credit it when you publish)
  - KJV and WEB (public domain English), used only to recognise verses the speaker quotes.
"""
import io, json, os, re, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "bible")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Wispher-fetch-bible"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def main():
    os.makedirs(OUT, exist_ok=True)
    for tid in ("tel2017", "eng-kjv", "eng-web"):
        z = zipfile.ZipFile(io.BytesIO(get(f"https://ebible.org/Scriptures/{tid}_vpl.zip")))
        z.extract(f"{tid}_vpl.txt", OUT)
        print("downloaded", tid)
    # Telugu book names (long form) from the IRV's own USFM headings
    z = zipfile.ZipFile(io.BytesIO(get("https://ebible.org/Scriptures/tel2017_usfm.zip")))
    names = {}
    for n in z.namelist():
        m = re.match(r"\d+-(\w{3})tel2017\.usfm$", os.path.basename(n))
        if not m:
            continue
        txt = z.read(n).decode("utf-8-sig")
        t = re.search(r"^\\toc1\s+(.+)$", txt, re.M)
        if t:
            names[m.group(1)] = t.group(1).strip()
    with open(os.path.join(OUT, "books_te.json"), "w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "CREDIT.txt"), "w", encoding="utf-8") as f:
        f.write("Telugu Bible: Indian Revised Version (IRV) Telugu 2019, copyright 2017, 2019 Bridge Connectivity "
                "Solutions, CC BY-SA 4.0 (https://ebible.org/tel2017/). Verses are used unchanged; "
                "surrounding text is machine-translated.\n")
    print(len(names), "Telugu book names saved. Done.")


if __name__ == "__main__":
    main()
