import asyncio
from urllib.parse import quote_plus, urljoin

from playwright.async_api import async_playwright


QUERY = '"Principal Portfolios" "Bryan Kelly" PDF'


async def main():

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=False
        )

        page = await browser.new_page()

        search_url = (
            "https://html.duckduckgo.com/html/?q="
            + quote_plus(QUERY)
        )

        await page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=60000
        )

        await page.wait_for_timeout(2000)

        # Extract unique search-result destinations
        results = []

        for a in await page.locator("a").all():

            try:

                text = (await a.inner_text()).strip()
                href = await a.get_attribute("href")

                if not href or "uddg=" not in href:
                    continue

                from urllib.parse import urlparse, parse_qs

                params = parse_qs(
                    urlparse(href).query
                )

                destination = params.get(
                    "uddg",
                    [None]
                )[0]

                if destination:

                    item = (
                        text,
                        destination
                    )

                    if item not in results:
                        results.append(item)

            except Exception:
                pass

        print("\nSEARCH RESULTS")
        print("=" * 80)

        for i, (text, url) in enumerate(results):

            print(f"\n[{i}]")
            print("TEXT:", text[:250])
            print("URL:", url)

        # Find the Wiley result
        wiley = None

        for text, url in results:

            if "wiley.com" in url.lower():

                wiley = url
                break

        if not wiley:

            print("\nWiley result not found.")
            await browser.close()
            return

        print("\n\nOPENING WILEY:")
        print(wiley)

        await page.goto(
            wiley,
            wait_until="domcontentloaded",
            timeout=60000
        )

        await page.wait_for_timeout(5000)

        print("\nWILEY PAGE")
        print("=" * 80)
        print("TITLE:", await page.title())
        print("URL:", page.url)

        print("\nPDF / FULL TEXT LINKS")
        print("=" * 80)

        for a in await page.locator("a").all():

            try:

                text = (await a.inner_text()).strip()
                href = await a.get_attribute("href")

                if not href:
                    continue

                href = urljoin(
                    page.url,
                    href
                )

                combined = (
                    text + " " + href
                ).lower()

                if (
                    "pdf" in combined
                    or "full text" in combined
                    or "download" in combined
                ):

                    print("\nTEXT:", repr(text))
                    print("URL:", href)

            except Exception:
                pass

        input("\nPress ENTER to close...")

        await browser.close()


asyncio.run(main())