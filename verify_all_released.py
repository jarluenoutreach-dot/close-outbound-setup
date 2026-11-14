"""Full-coverage re-check: verify EVERY one of the 78 originally-released leads
against the strict US/CA filter, using their known phone numbers directly
(from the apply_backlog.py output), regardless of whether the sweep has
touched them yet (many have no db row at all still). Anything that fails is
force-restored to baseline. Never touches a lead already 'sent' or 'error'.
"""
import re

from outbound.config import load_settings
from outbound.db import Db

# (lead_id, name, phone) for all 78 leads apply_backlog.py released.
RELEASED = [
    ("lead_7Ocp1GMl8xdNuGYcYDwz0NxdWlVY3gBFqx0fVuLwYij", "Erica", "+9013418019"),
    ("lead_earMG5PVFmDq7rnfMVNxcpG3w3Ex6GPGsuvMor9u44J", "Lonnie", "+15697667257"),
    ("lead_kXGTr0TNUol1SG57kJ29LYy8zCL88N0AUQwg2orY4aO", "Franklin", "+13572667312"),
    ("lead_2RcgQQiz03aEYdWBxJJHro5G0U70Pi0BNy9jqODeXig", "Dominique", "+150425500"),
    ("lead_LrIwevS4FKeQPp8aBA9l9ZDHm2unUTgjfLJGdTfrgIC", "Felicia", "+2013644776"),
    ("lead_ouwAjDt3HGNP17LA7SbFRV4uz5LPAeHWMJpjVMRw8AI", "Latrice", "+170471222905"),
    ("lead_HcQdcRjVi6TLsp0DLclVLQYCAhfxU1A1mAypbQph48m", "Delreese", "+13402777254"),
    ("lead_38q3sFHZkxo96Ip52JLgpWnbqvIv5m1MsKjapFDXCtJ", "Angela", "+16915439269"),
    ("lead_wRmtAl7hz1jXHIIuH8a1pKMhdVs4Dnn6zLElpS35P1k", "Amos", "+122272543464"),
    ("lead_DJpEvC3EVnfF6KgTZ2RcdxvoOpF0EYm5sg7B3EaYBVY", "Mike", "+16918620546"),
    ("lead_K7JSSCi40rTLFT3jZ0KD8ibfsQhzSdFYZcg7o1VYNQa", "Linda", "+150270941248"),
    ("lead_8yIvVVXdStC6FPYvhK2wJKDTtuLT6vv9kRQLa2AIrrN", "Martine", "+19952595890"),
    ("lead_PggvzgFbtg7NG4ttUd29Hx9afTmWlXoxIkZOEaz9IlF", "Jerry", "+192019172280"),
    ("lead_RTkFr78WFN75MCqjYvYRzV2AWBCmGmY3Osd3dHr7G04", "Irene", "+18964019459"),
    ("lead_Q7dLvRHictwiEbrPjcRaSp1cCfcgYiR5zHkTNLGX4ug", "Anita", "+19555510102"),
    ("lead_5RzpLqAKpacmKwFlauKIP7YHEXEc92L2xhU5uHsDjnD", "Chandra", "+160150609999"),
    ("lead_7DVTiP7Wdlh4njq5NX9DOwZYbVYRUhl82RLn8INuBNX", "Tou", "+16524245391"),
    ("lead_UMfDj44G5ssqnURImYXebfBHXLh7nCONJnwtCFGX6n5", "Cedric", "+12555085097"),
    ("lead_wOPfiY7JjcGfOIcGFeYN0pBrEdV73Sa7Qg7zUhfCF1t", "Alexis", "+1803542512663"),
    ("lead_t5jywRlyW9uJM9bMPYhtjTKMofJ0wQdFFN7xgziQi0p", "Riseanne", "+19499398946968"),
    ("lead_IkeLYyhvs9oAOyOtYm7AcTnEBP4NKVmN9sNdHme7UrB", "Emeka", "+100353861758441"),
    ("lead_E7UWZtUrkY2QuBZ08LB4RRV5VedV9qkyhKYW4kelvv5", "Devon", "+12477863073"),
    ("lead_tTK2kuL6M9UkXO6F8Z93s26QYv0tQXGt8dcuNwutzkL", "Donte", "+171377516669"),
    ("lead_vsCMnDdnAp3m9agDwGnbFCRrAeBLgXXAAfimcqQjKJw", "Jody", "+13336636699"),
    ("lead_FOuA07KJqn1smrmtgY5eFbMBfiFzzE48lVJSnIHt3dG", "Yaminah", "+15497693785"),
    ("lead_rh0pduyogqgOUyA9pIA7CebuVruFGRFJXclY6GHiKDy", "Dana", "+18946655404"),
    ("lead_5vfIyMk2RZn2TBM02aixGoh8f9LVVQLN3BZHyBNuLCk", "Leslie", "+14915788423"),
    ("lead_e3mcVwlkFlSsjXCkOD9yAOHP4nRcS6pz3TeMSv2Y7kT", "Bernadette", "+17976232722"),
    ("lead_2QCQ7ea4Jk0PFSKDbR8gS9nOk6hBhdpLS1FS9qFVARW", "Mavis", "+233243938682"),
    ("lead_3G5ozSOiXuUr2MQ8yCPJoD7iCR912SvcT0UNNVMAeV8", "Terra", "+17942589386"),
    ("lead_lQqS608ZtQxG3lcrqyzKMf0oM69BvltKVTMcJkquUCZ", "Vanessa", "+13577039628"),
    ("lead_hbHvPkf5hSQdHYOBycJqaKBZPoZdLM0EFnEnFfTqyi6", "Kyndall", "+131377017223"),
    ("lead_LBBZbK4560iLpX8FAd3NXiiJA3wQOMBy5XNp3OtBclH", "Horace", "+18463465146"),
    ("lead_f0VlW6LLKJl7TFtJJXGZm6HQeuUR88G2lajrhy4lpjr", "Michele", "+17982596364"),
    ("lead_y24PloqDB5ezcUZXjS4Kl4wbOl6ZzcdiFEUTHg2p53U", "Shannan", "+100000000000"),
    ("lead_JIBDpsPqCNCI0l2ltmSLZi4iv0uSqeKKSydBjtCERVA", "Clifton", "+18279130840"),
    ("lead_g6HmuvRhLZfZL6ZeRDvlkByN5dCum0ZkOCX4zVezkCE", "Lizbet", "+17965059697"),
    ("lead_SVuDIm7aZWA7y2Rt9aZIZSFSxxjcyyV4SfKk4ld76GN", "Pamela", "+18462811983"),
    ("lead_MsEdpqZWBRyzMAVCJDmLbURHDquW5JHjXKFAAikjK9q", "Johnathan", "+19269477587"),
    ("lead_T3tLLOuj4ebVwEqvH7IiH9xa8nb4dIvW6Or8s67gvwN", "Celeo", "+17945474244"),
    ("lead_6WdO4ON2HdcTTLjirO3CRnc69BWY4hYKQz2dXSvufHF", "Yamile", "+17965235773"),
    ("lead_atFwbI2uY4FdOzGZFsKLUu47QMfNNMoHsoIrRyWxJRr", "Sonya", "+17983621496"),
    ("lead_xkCmntrHknIrc1Xt6mOu8LAvgorhGXZe3Wn8kRLaNa0", "Nekeidra", "+16682297859"),
    ("lead_rwAUmKlWVO4LwTHa6rgHDcdj4VVl45HFFBEzMIVMNvD", "Christopher", "+14902725250"),
    ("lead_KGlOqH4P8JxNIO7ChpyqEMlPEyl2vBHMdVJxzYbzyFM", "Klea", "+124089365685"),
    ("lead_cgXM1Vv6fqpGXoVeQStHdrqGXKNL2GgsPHL83ZODxTG", "Kia", "+14442725554"),
    ("lead_2jR19hHggAEGeRiiRgIOAckziQwCLc8v8weBMIZLYkC", "Sherifat", "+109033929382"),
    ("lead_jmjNRnPFJyrWqeOOVdZmGilI9mciNG53tC1zyvQWwwG", "David", "+16214079084"),
    ("lead_xJ6oP07r9KOSVpV39SWC0TV7ozOcrQ53iJ3f6CcMD8a", "Reginald", "+19276411690"),
    ("lead_EDFO3a7bEHHqN1ftu1FEH40Ebk1epXTXQh2ksAdqn7G", "Victoria", "+17235380600"),
    ("lead_rUoPiQoZDjQ7MdX0acuVKBROQgqFvRyg6g5WGPHArZL", "Ayomide", "+14447208249"),
    ("lead_ZseVSE1JHTIicySpLxETgD5dh9d4U6HvuMomGp3H9AQ", "Unique", "+11234567"),
    ("lead_fkHD3iBiEEZuW9Z9FzdF4EfVqiIXF4cvi7TVKQJ4Edg", "Denise", "+16684885281"),
    ("lead_OWQkmCxX4UKOMrSgSRsZUcqueKw55a0bGQFzgtoTojJ", "Demarkos", "+167881697188"),
    ("lead_0xUQcYqZQaLY482GFup0cP6eiMEN2oF3Ao8oeZbkfUT", "Lovelyn", "+16442710488"),
    ("lead_f7v1nBf9ofo4VX6F7ovWqkdF704lUe7ji0IPXlCLTHN", "Addie", "+160113075081"),
    ("lead_lUqXbVN00jvCqpNImoin3C1Ys8m6bQubp8DzzY3pxK1", "Anthony", "+16243812484"),
    ("lead_xZ6EApWbrgzFym0YEzvI3L6y8l57Trqnwi1unk7JUhI", "Derrick", "+140792900401"),
    ("lead_f4AXjLjfZfjuRIaDQfZErzVlj9WLYxguRyXn7baHHfC", "primitivo sera joseph", "+191082438888"),
    ("lead_sKbxhmo8oRUMfwClo3pDFDrosdTAmkYEENaiskloi5C", "Byron", "+14599882613"),
    ("lead_F7TC0uGNqu3GDPfbX3hLZuiSPHCfBmSr2UglCrG30Og", "Shawn", "+194524303"),
    ("lead_0tzQBscx21p0rdAMJNviVB0hZ0bRXSF4tEYcwWQTV29", "Solly", "+27767120977"),
    ("lead_U7JHAWQSAgeTRAQpqyjPoz2iVggNcvs0pha7qjpW0ae", "Michael", "+124005067894"),
    ("lead_teFypBiLegjkGrHFm3yTKsD3cTpIpxpT4l3HP0bIehx", "Solomon", "+6583870756"),
    ("lead_HkIAcqdB3Msa6xsKYZn1fRPA3HG0J6cpgNnaeGFlUkp", "Bill", "+16529985801"),
    ("lead_ABsXT45atKkPW4Z6BMg1LXR1pXErvJCdk1j4v7ok2vo", "Hakim beja", "+177741637"),
    ("lead_IghxAFWJsGHKOuBMiN2X2MVz4QA9qEDQMDDrw1Fsbta", "Chalima", "+13409985760"),
    ("lead_CUJHrsmHTqbMhzA4NlOYwS0eOtGr92DYslcbWxVkWqc", "SIRREA", "+19247205008"),
    ("lead_MQrug8jvc28GX6q4qmcG8jVbGwnaM0F7rLbQEpVvtSE", "Martha", "+131364686"),
    ("lead_TG3W0auOW2YFESJJpOaRRf0lqzFMJW7FkwpF32KCDkW", "Anthony", "+173770233"),
    ("lead_Hk8HPxyVo4SmQtTVNxp4o0ezl1HVT17tKwFIGAkkLfB", "OJO", "+107079510135"),
    ("lead_TB2cM5gZfqMg9zE12HqRRF1FTTYVKzzgwSjlQa3BvdX", "Yigerem", "+15786814433"),
    ("lead_UK0mp9DFcwkCpYf20t9C2snMdyldjKjGZMp8grSd6Hv", "Freeman", "+17922966315"),
    ("lead_68SBw24oAa8Ntnwl5ybSte49EIIYgEbBRjHyQKaEiFZ", "Chichi", "+12348143550616"),
    ("lead_UrfIFxxQltVRnXO6eBLs3yzufbJh3crvmCjmbdB5MaF", "Ruzio", "+151086017220"),
    ("lead_gt3vTQcc4UccsiBpsOTAUfe3rrxNDkqyCPtk0wVYmcg", "Toya", "+131317368814"),
    ("lead_iKGWk39fKbD6edGQ2kEie1060eyxMv6rDYsoKPQEQRm", "Chakia", "+198057885"),
    ("lead_2nf8OsUh9sTCxyazAyyKWkajBRF6jqhk3uAlI1Cd3NT", "Alexander", "+140799900561"),
]


def strict_us_ca_number(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return False
    area, exch = digits[:3], digits[3:6]
    if area[0] in "01" or exch[0] in "01":
        return False
    if exch == "555" or len(set(digits)) == 1 or digits == "1234567890":
        return False
    return True


settings = load_settings()
db = Db(settings.db_path)

print(f"Re-checking all {len(RELEASED)} originally-released leads by their known phone\n")
restored, kept, left_alone = [], [], []
for lead_id, name, phone in RELEASED:
    rec = db.get(lead_id)
    if rec and rec["status"] in ("sent", "error"):
        left_alone.append((name, phone, rec["status"]))
        continue
    if strict_us_ca_number(phone):
        kept.append((name, phone))
    else:
        db.upsert(lead_id, status="baseline", source="baseline",
                  skip_reason="restored to baseline: phone failed the stricter US/CA check")
        restored.append((name, phone))

print(f"Left alone (already sent/errored, historical fact): {len(left_alone)}")
for name, phone, status in left_alone:
    print(f"  {name:<28} {phone:<16} {status}")
print(f"\nNewly restored to baseline (bad number): {len(restored)}")
for name, phone in restored:
    print(f"  {name:<28} {phone}")
print(f"\nConfirmed genuinely valid, still held for resume: {len(kept)}")
for name, phone in kept:
    print(f"  {name:<28} {phone}")
