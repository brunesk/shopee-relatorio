import asyncio
import os
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


def clean_store_url(url: str) -> str:
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if not parts or not parts[0]:
        raise ValueError("Link inválido. Use o formato: https://shopee.com.br/nomeddaloja")
    username = parts[0]
    return f"https://shopee.com.br/{username}", username


def parse_item(item: dict) -> dict | None:
    nome = item.get("name", "").strip()
    if not nome:
        return None

    price = item.get("price", 0)
    if isinstance(price, (int, float)) and price > 100000:
        price = price / 100000
    price = round(float(price), 2)

    sold = int(item.get("sold", 0))

    taxa_shopee = 0.20
    recebido = price * (1 - taxa_shopee)

    return {
        "nome": nome,
        "preco": price,
        "vendas_30d": sold,
        "historico_vendas": int(item.get("historical_sold", 0)),
        "avaliacao": round(float(item.get("item_rating", {}).get("rating_star", 0)), 1),
        "faturamento_30d": round(price * sold, 2),
        "preco_compra_30pct": round(recebido * 0.70, 2),
        "preco_compra_40pct": round(recebido * 0.60, 2),
        "vendas_por_dia": round(sold / 30, 1),
    }


def extract_items_from_response(data: dict) -> list:
    items = []

    # Formato recommend/recommend
    sections = data.get("data", {}).get("sections", [])
    for section in sections:
        for item in section.get("data", {}).get("item", []):
            parsed = parse_item(item)
            if parsed:
                items.append(parsed)

    # Formato search_items
    if not items:
        for wrap in data.get("items", []):
            item = wrap.get("item_basic", wrap)
            parsed = parse_item(item)
            if parsed:
                items.append(parsed)

    # Formato data.items
    if not items:
        for item in data.get("data", {}).get("items", []):
            parsed = parse_item(item)
            if parsed:
                items.append(parsed)

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


async def scrape_and_analyze(url: str) -> dict:
    store_url, username = clean_store_url(url)
    sorted_url = store_url + "?page=0&sortBy=sales&tab=0"

    products = []
    shop_name = username
    captured = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-setuid-sandbox",
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

        async def on_response(response):
            u = response.url
            if any(k in u for k in ["recommend/recommend", "search_items", "rcmd_items", "get_shop_item"]):
                try:
                    if response.status == 200:
                        data = await response.json()
                        captured.append(data)
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            await page.goto(sorted_url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            await browser.close()
            raise ValueError(f"Não foi possível acessar a loja. Verifique o link. ({e})")

        # Aguarda as chamadas de API carregarem
        await asyncio.sleep(6)

        # Tenta capturar nome da loja da página
        for selector in [".shop-name-content", "[class*='shopName']", ".seller-name", "h1"]:
            try:
                el = await page.query_selector(selector)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and len(text) < 80:
                        shop_name = text
                        break
            except Exception:
                pass

        await browser.close()

    for data in captured:
        items = extract_items_from_response(data)
        products.extend(items)

    # Remove duplicatas por nome
    seen = set()
    unique = []
    for p in products:
        if p["nome"] not in seen:
            seen.add(p["nome"])
            unique.append(p)
    products = unique

    if not products:
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
