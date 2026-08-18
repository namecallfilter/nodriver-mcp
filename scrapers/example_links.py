"""Example hot-reloadable scraper: collect links from the current page."""

import json


async def scrape(ctx, params):
    if url := params.get("url"):
        await ctx.goto(str(url))

    selector = str(params.get("selector", "a[href]"))
    limit = max(1, min(int(params.get("limit", 50)), 500))
    expression = """(() => {
        const selector = __SELECTOR__;
        const limit = __LIMIT__;
        const nodes = Array.from(document.querySelectorAll(selector));
        return {
            page: {title: document.title, url: location.href},
            total_matches: nodes.length,
            links: nodes.slice(0, limit).map((node) => ({
                text: String(node.innerText || node.textContent || '').trim(),
                href: node.href || null
            }))
        };
    })()"""
    expression = expression.replace("__SELECTOR__", json.dumps(selector)).replace(
        "__LIMIT__", str(limit)
    )
    return await ctx.evaluate_json(expression)
