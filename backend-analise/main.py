import asyncio
import os
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
        raise ValueError("Link inválido. Use o formato: https://shopee.com.br/nomeddaloja")
    username = parts[0]
    return f"https://shopee.com.br/{username}", username


def parse_item(item: dict):
    try:
        nome = (item.get("name") or "").strip()
        if not nome:
            return None

        price = item.get("price") or 0
        if isinstance(price, (int, float)) and price > 100000:
            price = price / 100000
        price = round(float(price or 0), 2)

        sold = int(item.get("sold") or 0)

        taxa_shopee = 0.20
        recebido = price * (1 - taxa_shopee)

        rating_obj = item.get("item_rating") or {}
        avaliacao = round(float(rating_obj.get("rating_star") or 0), 1)

        # Inclui shopid para filtrar depois
        shopid = item.get("shopid") or item.get("shop_id")

        return {
            "nome": nome,
            "preco": price,
            "vendas_30d": sold,
            "historico_vendas": int(item.get("historical_sold") or 0),
            "avaliacao": avaliacao,
            "faturamento_30d": round(price * sold, 2),
            "preco_compra_30pct": round(recebido * 0.70, 2),
            "preco_compra_40pct": round(recebido * 0.60, 2),
            "vendas_por_dia": round(sold / 30, 1),
            "_shopid": shopid,
        }
    except Exception as e:
        print(f"[PARSE_ITEM] Erro: {e} | item: {str(item)[:100]}")
        return None


def as_dict(val) -> dict:
    return val if isinstance(val, dict) else {}

def as_list(val) -> list:
    return val if isinstance(val, list) else []

def extract_items_from_response(data) -> list:
    if not isinstance(data, dict):
        return []
    items = []

    # Formato 1: recommend/recommend — data.sections[].data.item[]
    data_obj = as_dict(data.get("data"))
    sections = as_list(data_obj.get("sections"))
    for section in sections:
        if not isinstance(section, dict):
            continue
        section_data = as_dict(section.get("data"))
        for item in as_list(section_data.get("item")):
            if isinstance(item, dict):
                parsed = parse_item(item)
                if parsed:
                    items.append(parsed)

    # Formato 2: search_items — items[].item_basic
    if not items:
        for wrap in as_list(data.get("items")):
            if not isinstance(wrap, dict):
                continue
            item = wrap.get("item_basic")
            item = item if isinstance(item, dict) else wrap
            parsed = parse_item(item)
            if parsed:
                items.append(parsed)

    # Formato 3: data.items[]
    if not items:
        for item in as_list(data_obj.get("items")):
            if isinstance(item, dict):
                parsed = parse_item(item)
                if parsed:
                    items.append(parsed)

    # Formato 4: lista no topo com campo "name"
    if not items:
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("name"):
                    parsed = parse_item(item)
                    if parsed:
                        items.append(parsed)

    # Formato 5: qualquer chave que contenha uma lista de produtos
    if not items:
        for key in ["item", "products", "result", "list", "data"]:
            lst = as_list(data.get(key))
            for item in lst:
                if isinstance(item, dict) and item.get("name"):
                    parsed = parse_item(item)
                    if parsed:
                        items.append(parsed)
            if items:
                break

    return items


def gerar_insights(products: list, total_fat: float) -> list:
    insights = []
    if not products:
        return insights

    top3 = products[:3]
    fat_top3 = sum(p["faturamento_30d"] for p in top3)
    pct = (fat_top3 / total_fat * 100) if total_fat > 0 else 0

    nomes_top3 = ", ".join(p["nome"][:30] for p in top3[:2])
    insights.append({
        "tipo": "info",
        "titulo": f"Top 3 produtos = {pct:.0f}% do faturamento",
        "descricao": f"{nomes_top3} lideram as vendas e respondem por {pct:.0f}% do faturamento estimado.",
    })

    best = products[0]
    if best["vendas_por_dia"] > 0:
        insights.append({
            "tipo": "positivo",
            "titulo": f'"{best["nome"][:40]}" vende {best["vendas_por_dia"]:.1f}x por dia',
            "descricao": (
                f'Para entrar neste produto com 30% de margem: compre por até R${best["preco_compra_30pct"]:.2f}. '
                f'Para 40% de margem: até R${best["preco_compra_40pct"]:.2f}. Preço de venda atual: R${best["preco"]:.2f}.'
            ),
        })

    alta_vel = [p for p in products if p["vendas_30d"] >= 100]
    if alta_vel:
        insights.append({
            "tipo": "atencao",
            "titulo": f"{len(alta_vel)} produto(s) com 100+ vendas no mês — alta demanda",
            "descricao": "Produtos com alto volume indicam forte demanda no mercado. São boas oportunidades para copiar.",
        })

    baixa_vel = [p for p in products if 5 <= p["vendas_30d"] <= 30]
    if baixa_vel:
        insights.append({
            "tipo": "info",
            "titulo": f"{len(baixa_vel)} produto(s) com vendas moderadas — menos concorrência",
            "descricao": "Produtos com 5-30 vendas/mês podem ter menos disputa. Bom ponto de entrada para iniciantes.",
        })

    return insights


async def get_shop_info(username: str) -> tuple:
    """Busca shop_id e nome da loja via API pública da Shopee."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        "Referer": f"https://shopee.com.br/{username}",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://shopee.com.br/api/v4/shop/get_shop_detail?username={username}",
            headers=headers,
        )
        data = resp.json()
        shop_data = as_dict(data.get("data"))
        shop_id = shop_data.get("shopid") or shop_data.get("shop_id")
        shop_name = shop_data.get("name") or username
        return shop_id, shop_name


async def scrape_and_analyze(url: str) -> dict:
    store_url, username = clean_store_url(url)
    sorted_url = store_url + "?page=0&sortBy=sales&tab=0"

    # Passo 1: busca shop_id via API pública (sabemos que funciona)
    print(f"[SCRAPER] Buscando shop_id para: {username}")
    shop_id, shop_name = await get_shop_info(username)
    if not shop_id:
        raise ValueError("Loja não encontrada. Verifique o link.")
    print(f"[SCRAPER] shop_id={shop_id}, nome={shop_name}")

    products = []
    captured = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-setuid-sandbox",
                "--disable-web-security",
            ],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
        )

        page = await context.new_page()
        shop_id_str = str(shop_id)

        async def on_response(response):
            u = response.url
            # Captura toda resposta JSON da Shopee
            if (response.status == 200
                    and "shopee.com.br" in u
                    and "/api/" in u):
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        data = await response.json()
                        captured.append((u, data))
                        print(f"[CAPTURADO] {u[:120]}")
                except Exception as e:
                    print(f"[ERRO JSON] {u[:80]} -> {e}")

        # Homepage primeiro para pegar cookies de sessão
        print("[SCRAPER] Obtendo cookies da homepage...")
        try:
            await page.goto("https://shopee.com.br/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[AVISO] Homepage: {e}")

        print(f"[SCRAPER] Acessando loja: {sorted_url}")
        try:
            await page.goto(sorted_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_load_state("load", timeout=30000)
        except Exception as e:
            print(f"[AVISO] Load: {e}")

        await asyncio.sleep(5)

        # Chama a API da Shopee de DENTRO da página (tem cookies válidos, não dá 403)
        print(f"[SCRAPER] Chamando API de produtos de dentro da página...")
        endpoints = [
            f"/api/v4/recommend/recommend?bundle=shop_page_product_tab_main&item_card=2&limit=100&offset=0&shop_id={shop_id}&sort_type=1&tab_name=populares",
            f"/api/v4/recommend/recommend?bundle=shop_page_product_tab_main&limit=100&offset=0&shop_id={shop_id}&sort_type=1",
            f"/api/v4/search/search_items?by=sales&limit=100&match_id={shop_id}&newest=0&order=desc&page_type=shop&scenario=PAGE_OTHERS&version=2",
        ]

        for endpoint in endpoints:
            try:
                result = await page.evaluate(f"""
                    async () => {{
                        try {{
                            const r = await fetch('{endpoint}', {{
                                headers: {{'x-requested-with': 'XMLHttpRequest'}}
                            }});
                            return await r.json();
                        }} catch(e) {{
                            return {{error: e.toString()}};
                        }}
                    }}
                """)
                if result and not result.get("error"):
                    captured.append((endpoint, result))
                    print(f"[API OK] {endpoint[:80]}")
                    items = extract_items_from_response(result)
                    if items:
                        print(f"[PRODUTOS] {len(items)} encontrados neste endpoint")
                        break
                else:
                    print(f"[API ERRO] {endpoint[:80]} -> {result}")
            except Exception as e:
                print(f"[EVAL ERRO] {e}")

        await browser.close()

    print(f"[SCRAPER] APIs capturadas: {len(captured)}")

    # Extrai produtos das respostas capturadas
    all_items = []
    for u, data in captured:
        items = extract_items_from_response(data)
        if items:
            print(f"[PRODUTOS] {len(items)} encontrados em: {u[:80]}")
        all_items.extend(items)

    # Filtra pelo shopid se disponível
    loja_items = [p for p in all_items if str(p.get("_shopid") or "") == shop_id_str]
    if not loja_items:
        loja_items = all_items

    # Remove campo interno e duplicatas por nome
    seen = set()
    unique = []
    for p in loja_items:
        p.pop("_shopid", None)
        if p["nome"] not in seen:
            seen.add(p["nome"])
            unique.append(p)
    products = unique

    if not products:
        print(f"[FALHA] Nenhum produto encontrado. URLs da Shopee capturadas:")
        for u in all_urls:
            if "shopee" in u:
                print(f"  {u[:120]}")
        raise ValueError(
            "Nenhum produto encontrado. Verifique se o link é de uma loja Shopee válida e tente novamente."
        )

    products.sort(key=lambda x: x["faturamento_30d"], reverse=True)

    total_fat = sum(p["faturamento_30d"] for p in products)
    total_vendas = sum(p["vendas_30d"] for p in products)

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
