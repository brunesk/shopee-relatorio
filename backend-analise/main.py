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


class AnalyzeRequest(BaseModel):
    url: str


@app.get("/")
async def root():
    return {"status": "ok", "message": "Shopee Analyzer API funcionando"}


@app.post("/analisar")
async def analisar(req: AnalyzeRequest):
    try:
        resultado = await scrape_and_analyze(req.url)
        return resultado
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
        raise ValueError("Link inválido. Use o formato: https://shopee.com.br/nomedadaloja")
    return f"https://shopee.com.br/{parts[0]}", parts[0]


async def get_shop_detail(username: str) -> dict:
    """Busca shop_id e nome via API pública."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://shopee.com.br/api/v4/shop/get_shop_detail?username={username}",
            headers={"User-Agent": UA, "Referer": f"https://shopee.com.br/{username}"},
        )
        data = resp.json().get("data") or {}
        return {
            "shop_id": data.get("shopid") or data.get("shop_id"),
            "name": data.get("name") or username,
        }


async def get_browser_cookies(store_url: str) -> str:
    """Abre o Playwright só para obter cookies de sessão da Shopee."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        context = await browser.new_context(
            user_agent=UA,
            viewport={"width": 1366, "height": 768},
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
        )
        page = await context.new_page()

        # Visita homepage para gerar SPC_F e outros cookies de sessão
        print("[COOKIES] Acessando homepage...")
        try:
            await page.goto("https://shopee.com.br/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(4)
        except Exception as e:
            print(f"[COOKIES] Homepage erro: {e}")

        # Visita a loja para cookies específicos do contexto
        print(f"[COOKIES] Acessando loja: {store_url}")
        try:
            await page.goto(store_url, wait_until="domcontentloaded", timeout=40000)
            await asyncio.sleep(4)
        except Exception as e:
            print(f"[COOKIES] Loja erro: {e}")

        cookies = await context.cookies()
        await browser.close()

        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        print(f"[COOKIES] {len(cookies)} cookies obtidos")
        return cookie_str


def find_products_recursive(obj, results=None, depth=0):
    """Varre recursivamente o JSON procurando objetos com 'name' e 'price'."""
    if results is None:
        results = []
    if depth > 10:
        return results

    if isinstance(obj, dict):
        name = obj.get("name") or obj.get("item_name") or ""
        price = obj.get("price") or obj.get("price_min") or 0
        sold = obj.get("sold") or obj.get("sold_count") or 0

        if name and isinstance(name, str) and len(name) > 3 and price:
            if isinstance(price, (int, float)) and price > 100000:
                price = price / 100000
            results.append({
                "nome": name.strip(),
                "preco": round(float(price), 2),
                "vendas_30d": int(sold),
                "shopid": obj.get("shopid") or obj.get("shop_id"),
            })
        else:
            for v in obj.values():
                find_products_recursive(v, results, depth + 1)

    elif isinstance(obj, list):
        for item in obj:
            find_products_recursive(item, results, depth + 1)

    return results


async def fetch_products(shop_id: int, store_url: str, cookies: str) -> list:
    """Chama as APIs da Shopee com cookies reais do navegador."""
    headers = {
        "User-Agent": UA,
        "Cookie": cookies,
        "Referer": store_url,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
    }

    endpoints = [
        f"https://shopee.com.br/api/v4/recommend/recommend?bundle=shop_page_product_tab_main&item_card=2&limit=100&offset=0&shop_id={shop_id}&sort_type=1",
        f"https://shopee.com.br/api/v4/recommend/recommend?bundle=shop_page_product_tab_main&limit=100&offset=0&shop_id={shop_id}&sort_type=1&tab_name=populares",
        f"https://shopee.com.br/api/v4/search/search_items?by=sales&limit=100&match_id={shop_id}&newest=0&order=desc&page_type=shop&scenario=PAGE_OTHERS&version=2",
        f"https://shopee.com.br/api/v4/recommend/recommend?bundle=shop_page_product_tab_main&limit=100&offset=0&shop_id={shop_id}&sort_type=2",
    ]

    shop_id_str = str(shop_id)

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for endpoint in endpoints:
            try:
                resp = await client.get(endpoint, headers=headers)
                print(f"[API] Status {resp.status_code} → {endpoint[:80]}")

                if resp.status_code != 200:
                    continue

                data = resp.json()
                all_items = find_products_recursive(data)
                print(f"[API] {len(all_items)} itens totais no JSON")

                # Filtra por shopid
                shop_items = [i for i in all_items if str(i.get("shopid") or "") == shop_id_str]
                print(f"[API] {len(shop_items)} itens desta loja (shopid={shop_id_str})")

                if shop_items:
                    return shop_items
                elif all_items:
                    # Se não tem shopid no JSON mas encontrou itens, retorna todos
                    # (provavelmente endpoint específico da loja)
                    return all_items

            except Exception as e:
                print(f"[API ERRO] {e}")

    return []


def calcular_margens(products: list) -> list:
    resultado = []
    for p in products:
        preco = p["preco"]
        sold = p["vendas_30d"]
        recebido = preco * 0.80
        resultado.append({
            "nome": p["nome"],
            "preco": preco,
            "vendas_30d": sold,
            "avaliacao": 0.0,
            "faturamento_30d": round(preco * sold, 2),
            "preco_compra_30pct": round(recebido * 0.70, 2),
            "preco_compra_40pct": round(recebido * 0.60, 2),
            "vendas_por_dia": round(sold / 30, 1),
        })
    return resultado


def gerar_insights(products: list, total_fat: float) -> list:
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
                f'Preço de venda atual: R${best["preco"]:.2f}.'
            ),
        })

    alta_vel = [p for p in products if p["vendas_30d"] >= 100]
    if alta_vel:
        insights.append({
            "tipo": "atencao",
            "titulo": f"{len(alta_vel)} produto(s) com 100+ vendas no mês",
            "descricao": "Produtos com alto volume indicam forte demanda. São boas oportunidades para copiar.",
        })

    baixa_vel = [p for p in products if 5 <= p["vendas_30d"] <= 30]
    if baixa_vel:
        insights.append({
            "tipo": "info",
            "titulo": f"{len(baixa_vel)} produto(s) com vendas moderadas",
            "descricao": "Produtos com 5-30 vendas/mês costumam ter menos concorrência. Bom ponto de entrada.",
        })

    return insights


async def scrape_and_analyze(url: str) -> dict:
    store_url, username = clean_store_url(url)

    # 1. Busca shop_id via API pública
    print(f"[SCRAPER] Buscando info: {username}")
    info = await get_shop_detail(username)
    shop_id = info["shop_id"]
    shop_name = info["name"]
    if not shop_id:
        raise ValueError("Loja não encontrada. Verifique o link.")
    print(f"[SCRAPER] shop_id={shop_id}, nome={shop_name}")

    # 2. Obtém cookies reais via Playwright
    cookies = await get_browser_cookies(store_url)

    # 3. Chama a API com os cookies
    raw_products = await fetch_products(shop_id, store_url, cookies)

    if not raw_products:
        raise ValueError(
            "Não foi possível carregar os produtos desta loja. Tente novamente em alguns instantes."
        )

    # 4. Remove duplicatas
    seen = set()
    unique = []
    for p in raw_products:
        key = p["nome"].lower()[:50]
        if key not in seen:
            seen.add(key)
            unique.append(p)

    # 5. Calcula margens
    products = calcular_margens(unique)
    products.sort(key=lambda x: x["faturamento_30d"], reverse=True)

    total_fat = sum(p["faturamento_30d"] for p in products)
    total_vendas = sum(p["vendas_30d"] for p in products)

    print(f"[SCRAPER] Concluído: {len(products)} produtos, faturamento R${total_fat:.2f}")

    return {
        "loja": shop_name,
        "url": store_url,
        "total_produtos": len(products),
        "faturamento_30d": round(total_fat, 2),
        "total_vendas_30d": total_vendas,
        "melhor_produto": products[0] if products else None,
        "produtos": products,
        "insights": gerar_insights(products, total_fat),
    }
