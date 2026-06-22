import asyncio
import httpx
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI(title="Shopee Store Analyzer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

HEADERS_BASE = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "x-api-source": "pc",
    "x-shopee-language": "pt-BR",
    "x-requested-with": "XMLHttpRequest",
}


class AnalyzeRequest(BaseModel):
    url: str


@app.get("/")
async def root():
    return {"status": "ok", "message": "Shopee Analyzer API funcionando"}


@app.post("/analisar")
async def analisar(req: AnalyzeRequest):
    try:
        return await scrape_and_analyze(req.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro interno: {str(e)}")


def clean_store_url(url: str):
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if not parts or not parts[0]:
        raise ValueError("Link inválido. Use: https://shopee.com.br/nomedadaloja")
    return f"https://shopee.com.br/{parts[0]}", parts[0]


async def get_shop_id(username: str) -> tuple:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://shopee.com.br/api/v4/shop/get_shop_detail?username={username}",
            headers={**HEADERS_BASE, "Referer": "https://shopee.com.br/"},
        )
        data = resp.json().get("data") or {}
        shop_id = data.get("shopid") or data.get("shop_id")
        name = data.get("name") or username
        return shop_id, name


def parse_price(raw) -> float:
    if not isinstance(raw, (int, float)) or raw <= 0:
        return 0.0
    return raw / 100000 if raw > 100000 else float(raw)


def extract_products_from_items(items_list: list, shop_id_str: str) -> list:
    """
    Extrai produtos de uma lista de items do Shopee.
    Suporta tanto o formato direto quanto o formato {item_basic: {...}}.
    """
    result = []
    for item in items_list:
        if not isinstance(item, dict):
            continue

        # Formato search_items: {"item_basic": {...}}
        core = item.get("item_basic") or item

        if not isinstance(core, dict):
            continue

        name = core.get("name") or core.get("item_name") or ""
        price_raw = (
            core.get("price")
            or core.get("price_min")
            or core.get("price_max")
            or 0
        )
        sold = core.get("sold") or core.get("historical_sold") or 0
        shopid = core.get("shopid") or core.get("shop_id") or ""

        price = parse_price(price_raw)

        if not (name and isinstance(name, str) and len(name) > 3 and price > 0):
            continue

        if shop_id_str and str(shopid) and str(shopid) != shop_id_str:
            continue

        result.append({
            "nome": name.strip(),
            "preco": round(price, 2),
            "vendas_30d": int(sold),
            "_shopid": str(shopid),
        })

    return result


def find_items_array(obj, depth=0) -> list:
    """
    Procura recursivamente por um array de items/products no JSON.
    Para quando encontra um array com pelo menos 1 objeto que tenha name+price.
    """
    if depth > 8:
        return []

    if isinstance(obj, list):
        candidates = []
        for item in obj:
            if not isinstance(item, dict):
                continue
            core = item.get("item_basic") or item
            if isinstance(core, dict):
                name = core.get("name") or core.get("item_name") or ""
                price_raw = core.get("price") or core.get("price_min") or 0
                if name and isinstance(name, str) and len(name) > 3 and price_raw > 0:
                    candidates.append(item)
        if candidates:
            return candidates

        # Se o array não tem produtos diretos, procura nos filhos
        for item in obj:
            found = find_items_array(item, depth + 1)
            if found:
                return found

    elif isinstance(obj, dict):
        # Tenta chaves comuns primeiro para ser eficiente
        for key in ("items", "item_list", "products", "data", "result"):
            if key in obj:
                found = find_items_array(obj[key], depth + 1)
                if found:
                    return found
        # Depois tenta o resto
        for k, v in obj.items():
            if k in ("items", "item_list", "products", "data", "result"):
                continue
            found = find_items_array(v, depth + 1)
            if found:
                return found

    return []


async def fetch_products_direct(shop_id_str: str, username: str) -> list:
    """
    Tenta buscar produtos via HTTP direto no endpoint search_items da Shopee.
    Não precisa de browser — é o mesmo endpoint que a página usa.
    """
    print(f"[DIRETO] Tentando buscar produtos sem browser...")
    results = []

    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        offset = 0
        limit = 100
        max_pages = 5  # até 500 produtos

        while offset < limit * max_pages:
            params = {
                "by": "sales",
                "match_id": shop_id_str,
                "order": "desc",
                "page_type": "shop",
                "scenario": "PAGE_OTHERS",
                "version": "2",
                "limit": str(limit),
                "offset": str(offset),
            }
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"https://shopee.com.br/api/v4/search/search_items?{query}"

            try:
                resp = await client.get(
                    url,
                    headers={
                        **HEADERS_BASE,
                        "Referer": f"https://shopee.com.br/{username}",
                    },
                )
                print(f"[DIRETO] offset={offset} → HTTP {resp.status_code}")

                if resp.status_code != 200:
                    break

                data = resp.json()
                items_raw = find_items_array(data)
                if not items_raw:
                    break

                batch = extract_products_from_items(items_raw, shop_id_str)
                print(f"[DIRETO] {len(batch)} produtos nesta página")
                results.extend(batch)

                if len(items_raw) < limit:
                    break  # última página

                offset += limit

            except Exception as e:
                print(f"[DIRETO] Erro: {e}")
                break

    print(f"[DIRETO] Total: {len(results)} produtos")
    return results


async def fetch_products_browser(shop_id_str: str, username: str, store_url: str) -> list:
    """
    Fallback: usa Playwright e intercepta ESPECIFICAMENTE o endpoint
    search_items com page_type=shop — sem heurística de shopid.
    """
    print(f"[BROWSER] Iniciando Playwright...")
    sorted_url = store_url + "?page=0&sortBy=sales&tab=0"

    found_products = []
    search_done = asyncio.Event()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
        )
        page = await context.new_page()

        async def handle_response(response):
            u = response.url
            # Intercepta SOMENTE o endpoint de listagem da loja
            if (
                response.status == 200
                and "shopee.com.br" in u
                and "/api/" in u
                and (
                    ("search_items" in u and "page_type=shop" in u)
                    or ("search_items" in u and f"match_id={shop_id_str}" in u)
                    or ("get_shop_item_list" in u)
                    or ("shop/item" in u)
                )
            ):
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    data = await response.json()
                    items_raw = find_items_array(data)
                    if not items_raw:
                        return
                    batch = extract_products_from_items(items_raw, shop_id_str)
                    if batch:
                        print(f"[BROWSER] Capturei {len(batch)} produtos de: {u[:90]}")
                        found_products.extend(batch)
                        search_done.set()
                except Exception as e:
                    print(f"[BROWSER] Erro ao parsear resposta: {e}")

        page.on("response", handle_response)

        # Homepage para cookies
        try:
            await page.goto("https://shopee.com.br/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
        except Exception:
            pass

        # Página da loja
        print(f"[BROWSER] Carregando: {sorted_url}")
        try:
            await page.goto(sorted_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        # Aguarda até 15s pelo endpoint certo
        try:
            await asyncio.wait_for(search_done.wait(), timeout=15)
        except asyncio.TimeoutError:
            print("[BROWSER] Timeout — endpoint específico não apareceu")

        # Scroll para tentar carregar mais
        if not found_products:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.5)")
            await asyncio.sleep(3)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(3)

        await browser.close()

    print(f"[BROWSER] Total: {len(found_products)} produtos")
    return found_products


def calcular_margens(p: dict) -> dict:
    preco = p["preco"]
    sold = p["vendas_30d"]
    recebido = preco * 0.80
    return {
        "nome": p["nome"],
        "preco": preco,
        "vendas_30d": sold,
        "avaliacao": p.get("avaliacao", 0.0),
        "faturamento_30d": round(preco * sold, 2),
        "preco_compra_30pct": round(recebido * 0.70, 2),
        "preco_compra_40pct": round(recebido * 0.60, 2),
        "vendas_por_dia": round(sold / 30, 1),
    }


def gerar_insights(products, total_fat):
    insights = []
    if not products:
        return insights

    top3 = products[:3]
    fat_top3 = sum(p["faturamento_30d"] for p in top3)
    pct = (fat_top3 / total_fat * 100) if total_fat > 0 else 0
    nomes = ", ".join(p["nome"][:30] for p in top3[:2])
    insights.append({
        "tipo": "info",
        "titulo": f"Top 3 produtos = {pct:.0f}% do faturamento",
        "descricao": f"{nomes} lideram as vendas e respondem por {pct:.0f}% do faturamento estimado.",
    })

    best = products[0]
    if best["vendas_por_dia"] > 0:
        insights.append({
            "tipo": "positivo",
            "titulo": f'"{best["nome"][:40]}" vende {best["vendas_por_dia"]:.1f}x por dia',
            "descricao": (
                f'Para entrar com 30% de margem: compre por até R${best["preco_compra_30pct"]:.2f}. '
                f'Para 40% de margem: até R${best["preco_compra_40pct"]:.2f}. '
                f'Preço de venda: R${best["preco"]:.2f}.'
            ),
        })

    alta = [p for p in products if p["vendas_30d"] >= 100]
    if alta:
        insights.append({
            "tipo": "atencao",
            "titulo": f"{len(alta)} produto(s) com 100+ vendas no mês",
            "descricao": "Alta demanda — boa oportunidade para copiar.",
        })

    baixa = [p for p in products if 5 <= p["vendas_30d"] <= 30]
    if baixa:
        insights.append({
            "tipo": "info",
            "titulo": f"{len(baixa)} produto(s) com vendas moderadas",
            "descricao": "5-30 vendas/mês costumam ter menos concorrência. Bom ponto de entrada.",
        })

    return insights


async def scrape_and_analyze(url: str) -> dict:
    store_url, username = clean_store_url(url)

    print(f"[INFO] Buscando loja: {username}")
    shop_id, shop_name = await get_shop_id(username)
    if not shop_id:
        raise ValueError("Loja não encontrada. Verifique o link.")
    shop_id_str = str(shop_id)
    print(f"[INFO] shop_id={shop_id_str}, nome={shop_name}")

    # Camada 1: HTTP direto (sem browser)
    raw_products = await fetch_products_direct(shop_id_str, username)

    # Camada 2: Playwright com filtro de URL (fallback)
    if not raw_products:
        raw_products = await fetch_products_browser(shop_id_str, username, store_url)

    if not raw_products:
        raise ValueError(
            "Não foi possível carregar os produtos desta loja. Tente novamente."
        )

    # Remove duplicatas
    seen = set()
    unique = []
    for p in raw_products:
        key = p["nome"].lower()[:50]
        if key not in seen:
            seen.add(key)
            unique.append(calcular_margens(p))

    unique.sort(key=lambda x: x["faturamento_30d"], reverse=True)

    total_fat = sum(p["faturamento_30d"] for p in unique)
    total_vendas = sum(p["vendas_30d"] for p in unique)

    print(f"[OK] {len(unique)} produtos únicos | faturamento R${total_fat:.2f}")

    return {
        "loja": shop_name,
        "url": store_url,
        "total_produtos": len(unique),
        "faturamento_30d": round(total_fat, 2),
        "total_vendas_30d": total_vendas,
        "melhor_produto": unique[0] if unique else None,
        "produtos": unique,
        "insights": gerar_insights(unique, total_fat),
    }
