# 🚀 Guia de Lançamento — Acelera Shopee Gestão Financeira

Preço: **R$149,90 · acesso 12 meses** · Pagamento: Mercado Pago (Pix ~0,49% / cartão até 12x)

## Como funciona o sistema de acesso

1. Cliente cria conta grátis no site (ou já tem)
2. Sem licença ativa → cai na tela "Ative seu acesso" com botão de compra
3. Botão abre o checkout do Mercado Pago
4. Pagamento aprovado → o webhook grava a licença no Firestore → acesso liberado na hora
5. Licença vale 12 meses; renovação soma +12 meses ao prazo
6. Reembolso/chargeback → acesso cortado automaticamente

Admin: `brunoferrack@gmail.com` entra sempre, sem licença.

---

## ✅ Checklist do Bruno (nesta ordem)

### 1. Mercado Pago (~10 min)
- [ ] Entrar em **mercadopago.com.br** → menu **Suas integrações** (ou developers.mercadopago.com)
- [ ] **Criar aplicação** → nome: `Acelera Gestao Financeira` → tipo: Pagamentos online → CheckoutPro
- [ ] Copiar o **Access Token de PRODUÇÃO** (começa com `APP_USR-`)
- [ ] Copiar também o de **TESTE** (começa com `TEST-`) para testarmos antes

### 2. Chave do Firebase (~5 min)
- [ ] **console.firebase.google.com** → projeto `shopee-relatorio`
- [ ] ⚙️ Configurações do projeto → aba **Contas de serviço**
- [ ] Botão **Gerar nova chave privada** → baixa um arquivo `.json`
- [ ] Me mandar esse arquivo (ou guardar — vamos converter pra base64 juntos)

### 3. Vercel (~10 min)
- [ ] Criar conta em **vercel.com** com seu GitHub
- [ ] New Project → importar o repositório `shopee-relatorio`
- [ ] **Root Directory**: `api-pagamentos`
- [ ] Environment Variables:
  - `MP_ACCESS_TOKEN` = token do passo 1 (teste primeiro, produção depois)
  - `FIREBASE_SERVICE_ACCOUNT_B64` = o json do passo 2 em base64 (eu converto)
  - `SITE_URL` = `https://brunesk.github.io/shopee-relatorio`
  - `API_URL` = a URL que a Vercel der (ex: `https://acelera-pagamentos.vercel.app`)
- [ ] Deploy → me passar a URL final (se for diferente de `acelera-pagamentos.vercel.app`, eu atualizo no site)

### 4. Regras do Firestore (~2 min)
- [ ] Console Firebase → **Firestore Database** → aba **Regras**
- [ ] Apagar tudo e colar o conteúdo do arquivo `firestore.rules` deste repo
- [ ] **Publicar**

### 5. Teste final (juntos)
- [ ] Criar conta com email de teste → deve cair na tela de compra
- [ ] Comprar com o token de TESTE (cartão de teste do MP)
- [ ] Ver a licença aparecer no Firestore e o acesso liberar
- [ ] Trocar `MP_ACCESS_TOKEN` para o de PRODUÇÃO na Vercel → Redeploy
- [ ] 🚀 Lançar

---

## 🆘 Liberação manual (Pix direto na sua chave, taxa 0%)

Cliente pagou por fora? Libere na mão em 30 segundos:

1. Console Firebase → **Firestore Database** → coleção **`licencas`**
2. **Adicionar documento** → ID do documento = **email do cliente** (minúsculo)
3. Campos:
   - `status` (string) = `ativa`
   - `plano` (string) = `anual`
   - `expiraEm` (timestamp) = data daqui a 12 meses
4. Pronto — cliente clica em "↻ Já paguei" e entra

Para cortar acesso de alguém: mudar `status` para `cancelada`.
