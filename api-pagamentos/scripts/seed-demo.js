/**
 * Cria a conta demo (demo@acelerashopee.com.br / senha Demo2026!) com licença
 * e popula com dados reais processados da planilha de teste + 2 meses sintéticos,
 * replicando exatamente a lógica de processamento do index.html.
 *
 * Uso: node scripts/seed-demo.js caminho/da/chave.json caminho/da/planilha.xlsx
 */
const admin = require('firebase-admin')
const path = require('path')
const XLSX = require('xlsx')

const DEMO_EMAIL = 'demo@acelerashopee.com.br'
const DEMO_PASS = 'Demo2026!'

const keyPath = process.argv[2]
const xlsxPath = process.argv[3]
if (!keyPath || !xlsxPath) {
  console.log('uso: node scripts/seed-demo.js chave.json planilha.xlsx')
  process.exit(1)
}

admin.initializeApp({ credential: admin.credential.cert(require(path.resolve(keyPath))) })
const db = admin.firestore()

// ── réplica fiel da lógica do index.html ──
function pn(v) {
  if (v == null || v === '' || v === '-') return 0
  if (typeof v === 'number') return isNaN(v) ? 0 : v
  const f = parseFloat(String(v).replace(/\s/g, '').replace(',', '.'))
  return isNaN(f) ? 0 : f
}
function somaDescontoVendedor(r) {
  let s = 0
  for (const k in r) if (k === 'Desconto do vendedor' || k.startsWith('Desconto do vendedor_')) s += pn(r[k])
  return s
}

function processarPlanilha(rows, monthKeyOverride) {
  const validos = rows.filter(r => String(r['Status do pedido'] || '').trim() !== 'Cancelado' && String(r['Status do pedido'] || '').trim() !== '')
  const canc = rows.filter(r => String(r['Status do pedido'] || '').trim() === 'Cancelado')

  let subCheio = 0, bruto = 0, com = 0, svc = 0, trx = 0, frt = 0
  for (const r of validos) {
    const subLinha = pn(r['Subtotal do produto'])
    const tgLinha  = pn(r['Total global'])
    subCheio += subLinha
    bruto += tgLinha > 0 ? tgLinha : subLinha
    com += pn(r['Taxa de comissão líquida'])
    svc += pn(r['Taxa de serviço líquida'])
    trx += pn(r['Taxa de transação'])
    frt += pn(r['Taxa de envio pagas pelo comprador'])
  }
  const ofertas = Math.max(0, subCheio - bruto)
  const liquido = bruto - com - svc - trx
  const ratio   = subCheio > 0 ? bruto / subCheio : 1

  const datas = rows.map(r => { const d = new Date(r['Data de criação do pedido']); return isNaN(d) ? null : d }).filter(Boolean).sort((a, b) => a - b)
  const refDate = datas[Math.floor(datas.length / 2)] || new Date()
  const monthKey = monthKeyOverride || (refDate.getFullYear() + '-' + String(refDate.getMonth() + 1).padStart(2, '0'))

  const prodMap = {}
  for (const r of validos) {
    const n = String(r['Nome do Produto'] || 'Sem nome').trim()
    const v = String(r['Nome da variação'] || '').trim()
    const k = n + '\x01' + v
    if (!prodMap[k]) prodMap[k] = { name: n, variacao: v, qty: 0, valor: 0 }
    prodMap[k].qty += Math.max(1, pn(r['Quantidade']))
    prodMap[k].valor += pn(r['Subtotal do produto'])
  }
  const produtos = Object.values(prodMap).map(d => ({ name: d.name, variacao: d.variacao, qty: Math.round(d.qty), valor: +(d.valor * ratio).toFixed(2) }))

  const ufMap = {}
  for (const r of validos) {
    const u = String(r['UF'] || 'N/D').trim() || 'N/D'
    if (!ufMap[u]) ufMap[u] = { n: 0, valor: 0 }
    ufMap[u].n++
    ufMap[u].valor += pn(r['Subtotal do produto'])
  }
  const ufs = Object.entries(ufMap).map(([uf, d]) => ({ uf, n: d.n, valor: +(d.valor * ratio).toFixed(2) }))

  const stMap = {}
  for (const r of rows) {
    const st = String(r['Status do pedido'] || 'Desconhecido').trim()
    if (!stMap[st]) stMap[st] = { n: 0, valor: 0 }
    stMap[st].n++
    stMap[st].valor += pn(r['Subtotal do produto'])
  }
  const status = Object.entries(stMap).map(([st, d]) => ({ status: st, n: d.n, valor: +(d.valor * ratio).toFixed(2) }))

  const d1 = datas.length ? datas[0].toISOString().slice(0, 10) : ''
  const d2 = datas.length ? datas[datas.length - 1].toISOString().slice(0, 10) : ''

  return {
    monthKey, arquivo: path.basename(xlsxPath),
    periodoInicio: d1, periodoFim: d2,
    pedidos: validos.length, cancelados: canc.length,
    bruto, sub: bruto, subtotalCheio: subCheio, ofertas,
    frete: frt, descontos: ofertas,
    comissao: com, servico: svc, transacao: trx,
    liquido, adsGasto: 0,
    produtos, ufs, status,
    uploadedAt: admin.firestore.FieldValue.serverTimestamp(),
  }
}

// variação sintética de um mês (escala valores para simular histórico)
function variar(doc, monthKey, fator) {
  const v = JSON.parse(JSON.stringify(doc))
  v.monthKey = monthKey
  v.arquivo = 'Order.all.' + monthKey.replace('-', '') + '.xlsx'
  for (const k of ['bruto', 'frete', 'descontos', 'ofertas', 'subtotalCheio', 'comissao', 'servico', 'transacao', 'sub', 'liquido']) v[k] = +(v[k] * fator).toFixed(2)
  v.pedidos = Math.round(v.pedidos * fator)
  v.cancelados = Math.round(v.cancelados * fator)
  v.produtos = v.produtos.map(p => ({ ...p, qty: Math.max(1, Math.round(p.qty * fator)), valor: +(p.valor * fator).toFixed(2) }))
  v.ufs = v.ufs.map(u => ({ ...u, n: Math.max(1, Math.round(u.n * fator)), valor: +(u.valor * fator).toFixed(2) }))
  v.status = v.status.map(st => ({ ...st, n: Math.max(1, Math.round(st.n * fator)), valor: +(st.valor * fator).toFixed(2) }))
  v.uploadedAt = admin.firestore.FieldValue.serverTimestamp()
  // datas coerentes com o mês
  const [y, m] = monthKey.split('-')
  v.periodoInicio = `${y}-${m}-01`
  v.periodoFim = `${y}-${m}-28`
  v.adsGasto = +(v.liquido * 0.08).toFixed(2) // ads sintético ~8% do líquido
  return v
}

async function main() {
  // 1. conta demo no Auth
  let user
  try {
    user = await admin.auth().getUserByEmail(DEMO_EMAIL)
    console.log('conta demo já existe:', user.uid)
  } catch {
    user = await admin.auth().createUser({ email: DEMO_EMAIL, password: DEMO_PASS, emailVerified: true })
    console.log('conta demo criada:', user.uid)
  }
  await admin.auth().updateUser(user.uid, { password: DEMO_PASS })

  // 2. licença
  const expira = new Date(); expira.setFullYear(expira.getFullYear() + 5)
  await db.collection('licencas').doc(DEMO_EMAIL).set({
    status: 'ativa', plano: 'demo', origem: 'conta-demo-interna',
    expiraEm: admin.firestore.Timestamp.fromDate(expira),
    atualizadoEm: admin.firestore.FieldValue.serverTimestamp(),
  }, { merge: true })
  console.log('licença demo ativa até', expira.toLocaleDateString('pt-BR'))

  // 3. processa planilha real (maio) + gera abril e março sintéticos
  const wb = XLSX.readFile(xlsxPath)
  const rows = XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]], { defval: '' })
  const maio = processarPlanilha(rows)
  console.log(`mês real ${maio.monthKey}: ${maio.pedidos} pedidos · bruto ${maio.bruto.toFixed(2)} · líquido ${maio.liquido.toFixed(2)}`)

  const [y, m] = maio.monthKey.split('-').map(Number)
  const prev1 = `${m - 1 <= 0 ? y - 1 : y}-${String(m - 1 <= 0 ? 12 : m - 1).padStart(2, '0')}`
  const prev2 = `${m - 2 <= 0 ? y - 1 : y}-${String(m - 2 <= 0 ? 12 + (m - 2) : m - 2).padStart(2, '0')}`
  const abril = variar(maio, prev1, 0.82)
  const marco = variar(maio, prev2, 0.64)

  const userRef = db.collection('shopee_users').doc(user.uid)
  for (const doc of [marco, abril, maio]) {
    await userRef.collection('months').doc(doc.monthKey).set(doc)
    console.log('gravado mês', doc.monthKey)
  }

  // 4. custos de produto de exemplo (40% do valor médio unitário) + email p/ admin
  // Mesma lógica do app: produto com múltiplas variações ganha custo por variação
  const variantIdx = {}
  for (const p of maio.produtos) {
    if (!variantIdx[p.name]) variantIdx[p.name] = new Set()
    if (p.variacao) variantIdx[p.name].add(p.variacao)
  }
  const custoKeyFor = (name, variacao) => {
    const temMultiplas = variantIdx[name] && variantIdx[name].size > 1
    return (temMultiplas && variacao) ? name + ' — ' + variacao : name
  }
  const custos = {}
  for (const p of maio.produtos) {
    const unit = p.qty > 0 ? p.valor / p.qty : 0
    custos[custoKeyFor(p.name, p.variacao)] = +(unit * 0.4).toFixed(2)
  }
  const monthCount = 3
  const totalBruto = maio.bruto + abril.bruto + marco.bruto
  const totalLiquido = maio.liquido + abril.liquido + marco.liquido
  await userRef.set({ email: DEMO_EMAIL, custos, monthCount, totalBruto, totalLiquido }, { merge: true })
  console.log('custos de exemplo gravados para', Object.keys(custos).length, 'produtos')
  console.log('\n✅ Conta demo pronta:')
  console.log('   email:', DEMO_EMAIL)
  console.log('   senha:', DEMO_PASS)
}

main().then(() => process.exit(0)).catch(e => { console.error('❌', e); process.exit(1) })
