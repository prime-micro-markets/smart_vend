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


def test_internal_app_is_noindex(client):
    # An authenticated internal page (require_user is stubbed in conftest).
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert 'name="robots" content="noindex, nofollow"' in resp.text
