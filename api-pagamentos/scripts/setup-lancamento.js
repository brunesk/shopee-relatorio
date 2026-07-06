/**
 * Setup de lançamento — roda UMA vez com a chave de admin do Firebase.
 *
 * O que faz:
 *  1. Cria licença de 12 meses para os alunos pagantes (PAGANTES abaixo)
 *  2. Lista todas as contas e dados existentes
 *  3. Com --confirmar: APAGA dados e contas dos alunos teste (todos que não
 *     estão em PAGANTES nem são o admin)
 *
 * Uso:
 *   node scripts/setup-lancamento.js caminho/da/chave.json            (só mostra, não apaga)
 *   node scripts/setup-lancamento.js caminho/da/chave.json --confirmar (executa a limpeza)
 */
const admin = require('firebase-admin')
const path = require('path')

const ADMIN_EMAIL = 'brunoferrack@gmail.com'
const PAGANTES = ['oseias.melo.morais@gmail.com'] // alunos que já pagaram — mantêm acesso e dados
const MESES = 12

const keyPath = process.argv[2]
const confirmar = process.argv.includes('--confirmar')

if (!keyPath) {
  console.log('❌ Informe o caminho da chave: node scripts/setup-lancamento.js chave.json [--confirmar]')
  process.exit(1)
}

admin.initializeApp({ credential: admin.credential.cert(require(path.resolve(keyPath))) })
const db = admin.firestore()

async function main() {
  // 1) Licenças para os pagantes
  for (const email of PAGANTES) {
    const expira = new Date()
    expira.setMonth(expira.getMonth() + MESES)
    await db.collection('licencas').doc(email).set({
      status: 'ativa',
      plano: 'anual',
      origem: 'liberacao-manual-lancamento',
      pagoEm: admin.firestore.FieldValue.serverTimestamp(),
      expiraEm: admin.firestore.Timestamp.fromDate(expira),
      atualizadoEm: admin.firestore.FieldValue.serverTimestamp(),
    }, { merge: true })
    console.log(`✅ Licença ativa: ${email} (expira ${expira.toLocaleDateString('pt-BR')})`)
  }

  // limpa doc de teste de permissão, se existir
  await db.collection('licencas').doc('teste-permissao').delete().catch(() => {})

  // 2) Mapa de contas (Auth) e dados (Firestore)
  const authUsers = []
  let pageToken
  do {
    const res = await admin.auth().listUsers(1000, pageToken)
    authUsers.push(...res.users)
    pageToken = res.pageToken
  } while (pageToken)

  const manter = new Set([ADMIN_EMAIL, ...PAGANTES])
  console.log(`\n📋 ${authUsers.length} contas no Auth:`)
  const remover = []
  for (const u of authUsers) {
    const em = (u.email || '').toLowerCase()
    const status = manter.has(em) ? 'MANTER' : 'REMOVER (teste)'
    console.log(`   ${em || '(sem email)'} — ${status}`)
    if (!manter.has(em)) remover.push(u)
  }

  if (!confirmar) {
    console.log('\n⚠️  Modo visualização. Nada foi apagado.')
    console.log('    Para executar a limpeza: adicione --confirmar ao comando.')
    return
  }

  // 3) Limpeza dos alunos teste
  for (const u of remover) {
    const uid = u.uid
    // apaga subcoleção months
    const months = await db.collection('shopee_users').doc(uid).collection('months').listDocuments()
    for (const m of months) await m.delete()
    await db.collection('shopee_users').doc(uid).delete()
    await admin.auth().deleteUser(uid)
    console.log(`🗑️  Removido: ${u.email || uid}`)
  }

  console.log(`\n✅ Concluído: ${remover.length} contas teste removidas, ${PAGANTES.length} pagante(s) liberado(s).`)
}

main().then(() => process.exit(0)).catch((e) => { console.error('❌', e); process.exit(1) })
