"""SEO surface: robots.txt, sitemap.xml, indexability, and structured data.

These guard the marketing-facing crawl signals against regressions — the
internal app must stay out of the index while the landing page stays in it.
"""


def test_robots_txt_served_from_root(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # Public surfaces allowed, internal app disallowed, sitemap advertised.
    assert "Allow: /$" in body
    assert "Allow: /chatbot/" in body
    assert "Disallow: /dashboard" in body
    assert "Disallow: /equipment/" in body
    assert "Disallow: /customer-service/" in body
    assert "Sitemap: https://primemicromarkets.com/sitemap.xml" in body


def test_sitemap_xml_lists_homepage(client):
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert "xml" in resp.headers["content-type"]
    body = resp.text
    assert "<loc>https://primemicromarkets.com/</loc>" in body
    assert "<lastmod>" in body
    assert "urlset" in body


def test_landing_is_indexable_with_seo_tags(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    # Canonical + indexability
    assert '<link rel="canonical" href="https://primemicromarkets.com/">' in html
    assert 'name="robots" content="index, follow' in html
    # Social preview
    assert 'property="og:title"' in html
    assert 'name="twitter:card"' in html
    # Structured data
    assert 'application/ld+json' in html
    assert '"@type": "LocalBusiness"' in html


def test_landing_targets_local_keywords(client):
    html = client.get("/").text
    # Keyword + geo meta
    assert 'name="keywords"' in html
    assert 'name="geo.region" content="US-FL"' in html
    # Core service + location terms are present somewhere on the page
    for term in ("micro market", "smart cooler", "Panama City", "Bay County"):
        assert term in html, f"missing keyword: {term}"
    # "vending machines" is a primary search term — must appear in both the
    # visible copy and the metadata (case-insensitive to allow heading casing).
    assert "vending machine" in html.lower()
    # Panama City Beach targeted in the structured data area list + copy
    assert "Panama City Beach" in html


def test_h1_carries_primary_keywords(client):
    # The single H1 is the strongest on-page ranking signal.
    import re
    html = client.get("/").text
    h1 = re.search(r"<h1>(.*?)</h1>", html, re.S)
    assert h1, "landing page must have an <h1>"
    text = h1.group(1).lower()
    assert "vending machine" in text
    assert "smart cooler" in text
    assert "micro market" in text


def test_owner_name_not_in_public_metadata(client):
    # Personal name was intentionally removed from the public page.
    html = client.get("/").text
    assert "Troup" not in html
    assert "Stephen" not in html
    assert '"founder"' not in html


def test_internal_app_is_noindex(client):
    # An authenticated internal page (require_user is stubbed in conftest).
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert 'name="robots" content="noindex, nofollow"' in resp.text
