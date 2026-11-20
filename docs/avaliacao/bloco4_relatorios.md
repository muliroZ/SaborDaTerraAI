# Bloco 4: resumos, relatórios sob demanda, economia de tools e rascunhos

Execução do Bloco 4 da bateria de avaliação (`bateria_testes_cajai.pdf`, casos REL-01 a REL-08), em 19/11/2026, sobre o branch `dev` (`7b5c370`).

## Como foi testado

- Cada caso rodou por `processar_turno` (o mesmo caminho da interface) como **Dona Lúcia** (perfil equipe), numa sessão nova, sem memória e sem cache, com `DATA_REF_DATE=2026-10-30`.
- O turno foi instrumentado para guardar as tools chamadas, com argumentos e retornos, além da resposta final (texto, fontes, flags, rascunhos, relatórios e regras do guardrail).
- A bateria rodou **3 vezes**, porque o LLM não é determinístico. Todas as respostas vieram do `gemini-3.5-flash-lite`, sem fallback, sem falha segura e sem bloqueio do guardrail de saída.
- Os números e afirmações foram conferidos no banco (`financeiro`, `pedidos`, `triagem_pedidos`, `produto_sinonimos`), nos documentos de `data/facepe/` e em `docs/fontes/`.

## Placar

Legenda: ✅ atende ao esperado no PDF · ⚠️ atende ao esperado, mas com defeito relevante · ❌ não atende.

| ID | Pedido | Resultado | Resumo |
|---|---|---|---|
| REL-01 | Resumo da Maria das Graças + rascunho | ⚠️ | Usa só `resumir_produtor`. Vendas R$ 1.090 / R$ 0, pendente R$ 0, sem NC falsa e com o card de rascunho. Mas afirma "nenhum pedido elegível", o que é falso (P001 tem goiaba no PED-2026-002): achado [F1](#f1). |
| REL-02 | "Gere um relatório da Maria das Graças" | ⚠️ | Não cai em falha segura: o filtro de `prepare_tools` esconde `gerar_relatorio` e o pedido vai para `resumir_produtor`. Mas o modelo encadeia mais 2 ou 3 tools redundantes nas 3 rodadas: achado [F4](#f4), resolvido. |
| REL-03 | Resumo do P008 e do P004 | ⚠️ | P008 aparece com certificação vencida desde 30/09/2026 e P004 vencendo em 01/12/2026 (3/3). Em 1 das 3 rodadas, os rascunhos saíram com `\n` literal no card ([F3](#f3), resolvido). A flag `inadimplencia` foi aplicada à P004, que é a credora ([F6](#f6)). |
| REL-04 | Resumo do Cícero Romão (P003) | ❌ | Mostra R$ 6.020 vencidos, mas não cita a SuperBom (0/3) e diz "nenhum pedido elegível", quando existem pedidos abertos de queijo coalho, ricota e queijo manteiga: achados [F1](#f1) (resolvido) e [F5](#f5). |
| REL-05 | Relatório de contas a receber | ✅ | Chama `gerar_relatorio('contas_a_receber')` e anexa `contas_a_receber_20261030.md`. O texto traz R$ 8.690 vencidos, R$ 4.690 a vencer e R$ 13.380 no total, sem colar o Markdown. Tabelas conferidas linha a linha. |
| REL-06 | Levantamento de orgânicos vigentes para baixar | ✅ | Chama `gerar_relatorio('organicos_vigentes')` e anexa o `.md` com 8 produtores vigentes. P008 fica fora e aparece na seção "Atenção". |
| REL-07 | Dossiê ADAGRO do PED-2026-014 | ✅ | Anexa `dossie_adagro_PED-2026-014_20261030.md` com o que foi pedido, o que existe e o que falta, item a item. Confere com `docs/fontes/pedidos.md` (8 vigentes, laudos só de P002/P003, 3 lotes só de queijo coalho e mel, NC-001 descumprida, NC-002 sem evidência). |
| REL-08 | Rascunhos para os elegíveis do PED-2026-002 | ⚠️ | O bug `ctx: Any` citado no PDF **já foi corrigido** (`a698246`) e a tool roda. Mas a banana prata (40 kg) some sem aviso ([F2](#f2), resolvido), e o LLM reescreve os rascunhos da tool, inventando itens e afirmações ([F3](#f3), resolvido). |

**Placar para a apresentação:** 3 ✅ / 4 ⚠️ / 1 ❌. Pelo critério estrito do PDF, são **7/8**: só o REL-04 deixa de cumprir um item explícito. Os ⚠️ cumprem o que o PDF pede, mas têm defeitos que aparecem numa demo.

## Achados

Severidade: **alta** dá resposta errada ao operador numa função central; **média** dá conteúdo incorreto ou instável que a equipe pode encaminhar; **baixa** é qualidade ou rastreabilidade.

### F1

**`pedido_itens` está vazia, e `resumir_produtor` nunca encontra pedidos em aberto. Severidade: alta.**

- **Evidência:** `SELECT count(*) FROM pedido_itens` retorna `0`. Nas 3 rodadas, REL-01, REL-03 e REL-04 dizem "Pedidos em aberto: nenhum pedido elegível no momento", inclusive no rascunho enviado ao produtor.
- **Causa:** `src/sabor/etl/repo/EtlPedidoItens.py` é um esqueleto: `extract`, `transform` e `load` só registram log. A consulta de `src/sabor/tools/resumir_produtor.py:334` (passo 4) faz `JOIN pedido_itens` e por isso sempre volta vazia, para qualquer produtor.
- **Impacto:** o requisito do Pilar 4.3 ("pedidos alocados" no resumo) não funciona com os dados reais. O caso mais visível é o P003: ele tem 200 kg de queijo parado (PED-015), e há demanda aberta de queijo coalho em PED-001, 003, 005, 006 e 007, além de ricota (PED-001) e queijo manteiga (PED-006). P001 também tem goiaba pedida no PED-2026-002 e em outros pedidos.
- **Por que os testes não pegam:** `tests/tools/test_resumir_produtor.py` não verifica `pedidos_em_aberto`, e os 30 testes de resumo, relatórios e rascunhos passam.
- **Caminhos de correção:**
  - (a) Implementar `EtlPedidoItens`. As fontes não trazem itens estruturados (`pedidos.extracao` também está nula), então a carga teria de vir da extração da triagem em lote (`triagem_pedidos.resultado->'itens'`, que já tem `produto_id` e `produtores_elegiveis`).
  - (b) Fazer `resumir_produtor` ler os itens de `triagem_pedidos`, como `rascunhar_notificacao` já faz em `_pedido_de_triagem_salva`.
  - Nos dois casos, adicionar um teste de integração que cubra P003 com queijo.

**Status: resolvido**, numa variação do caminho (a). Os itens só existem depois da extração, então quem grava `pedido_itens` é a triagem, e não o ETL.

- `src/sabor/triagem.py`: `_gravar_itens` substitui os itens do pedido na mesma transação que grava `triagem_pedidos`, tanto na extração quanto na reclassificação sem LLM. Um erro apaga os itens.
- `src/sabor/tools/resumir_produtor.py`: o passo 4 junta `triagem_pedidos` (com `status = 'ok'`) para obter o tipo, a data de entrega e o comprador, que estão sempre nulos em `pedidos`. Itens ambíguos ficam de fora, como "aquele queijo da última vez" (PED-003).
- `EtlPedidoItens` agora só explica por que não há carga. A documentação está em `docs/dev/triagem_lote.md` e no README, que ganhou o passo da triagem depois do ETL.
- Testes novos em `tests/test_triagem.py`:
  - os itens vão para `pedido_itens`, são substituídos sem duplicar e somem com erro;
  - de ponta a ponta, da triagem ao `resumir_produtor`: queijo e ricota aparecem para o P003 com SIE, o item ambíguo não aparece, e o produtor sem SIE não recebe o pedido.
- **Validação no banco de desenvolvimento:** `triagem_lote.py --limite 0` reclassificou os 15 pedidos sem tokens e gravou 58 itens (47 no catálogo e 11 ambíguos).
  - P003: PED-001 (queijo coalho 30 kg e ricota 5 kg), PED-005 (queijo coalho 10 kg), PED-006 (queijo coalho 200 kg e queijo manteiga 50 kg), PED-007 (queijo coalho 25 kg e queijo manteiga 10 kg) e PED-013 (queijo coalho 50 kg).
  - P001: goiaba em PED-002, 004, 006 e 007.
  - P008 (orgânico vencido, sem SIE) e P017 (sem SIE) continuam sem pedidos.
  - Com o agente, o REL-04 passou a listar esses pedidos com o comprador de cada um.
- **Pontos que ficam em aberto:**
  - Um pedido com entrega já passada continua listado: o PED-006 é para 29/10 e a data de referência é 30/10.
  - O PED-006 é da SuperBom, que deve R$ 6.020 ao próprio P003, e o resumo não cruza essa informação. Isso entra junto com o [F5](#f5).
  - A fonte dos pedidos sai como `pedidos.csv`, mas o arquivo é `pedidos.json` (`resumir_produtor.py:380`). Isso entra junto com o [F8](#f8).

### F2

**A banana prata do PED-2026-002 é descartada sem aviso. Severidade: alta.**

- **Evidência:** a alocação do REL-08 cobre goiaba, maracujá, macaxeira, inhame, tomate, alface e rúcula, mas não a banana prata (40 kg, que é da P006, segundo `docs/fontes/pedidos.md:55`). Nenhuma das 3 respostas menciona banana.
- **Causa, em 3 camadas:**
  1. Na triagem (`triagem_pedidos`), o LLM de extração devolveu `produto: null, ambiguo: true` com a observação "banana prata não especificada no catálogo". Só que o catálogo tem `banana` (id 4) e o sinônimo `banana prata → 4` em `produto_sinonimos`.
  2. `src/sabor/tools/pedidos.py:278` pula o `catalogo.resolver(...)` determinístico quando o item já veio `ambiguo`. O sinônimo existente nunca chega a ser tentado.
  3. `src/sabor/tools/rascunhar_notificacao.py:275` ignora itens ambíguos (`continue`) e não os lista no texto da proposta. O operador não fica sabendo que 40 kg ficaram sem produtor.
- **Correção sugerida:**
  - Tentar o `resolver` também nos itens que o LLM marcou como ambíguos, quando `produto` vier nulo e `produto_texto` bater exatamente com um sinônimo. Referências ao histórico ("o de sempre") continuam ambíguas, porque não batem com nenhum sinônimo.
  - Fazer `rascunhar_notificacao` listar os itens não alocados, com o motivo.

**Status: resolvido** nas duas camadas que davam o sumiço.

- **Catálogo (camadas 1 e 2).** `normalizar_pedido` (`src/sabor/tools/pedidos.py`) agora tenta `Catalogo.resolver_exato` nos itens que o LLM marcou como ambíguos sem indicar produto.
  - Só o nome ou sinônimo exato desfaz a marcação. A correspondência por palavras ("frango" → frango caipira) não vale aqui, para não adivinhar o que o LLM considerou incerto.
  - "manteiga", "mel variado", "frutas variadas" e "o queijo da última vez" continuam ambíguos.
  - A observação original fica preservada depois de "Identificado no catálogo como X.", porque pode trazer outra informação, como "Fornecimento semanal".
- **Rascunhos (camada 3).** `rascunhar_notificacao` lista os itens ambíguos e os sem produtor elegível numa seção "Itens sem alocação", com o motivo, e cria uma flag para cada um (`ambiguidade` ou `sem_produtor_elegivel`).
- **Testes:**
  - `test_resolver_exato_usa_so_nome_ou_sinonimo` (catálogo);
  - `test_item_que_o_modelo_marcou_ambiguo_resolve_por_nome_ou_sinonimo_exato` (banana prata, Mel puro, manteiga, frango);
  - `test_itens_nao_alocados_aparecem_no_texto_e_nas_flags`.

  Os 10 casos falham sem a correção.
- **Validação no banco de desenvolvimento:** a reclassificação sem LLM resolveu 3 itens:
  - "banana prata" → banana (P006), no PED-002;
  - "tapioca/goma" → tapioca (P013), no PED-007;
  - "Mel puro" → mel silvestre (P002), no PED-009.
- **Validação com o agente:**
  - No REL-08 (3 rodadas), o rascunho da P006 traz "40 kg de banana; 25 kg de maracujá".
  - No PED-2026-007, a tool lista mel variado (ambíguo), frango caipira e ovos caipira (sem produtor elegível), e o texto do modelo repassa isso à equipe.
- **Pontos que ficam em aberto:**
  - "tapioca/goma" e "Mel puro" agora são resolvidos pelos sinônimos cadastrados (`tapioca/goma → tapioca`, `mel puro → mel silvestre`). Se a equipe não concordar com esses sinônimos, o ajuste é na tabela, não no código.
  - A unidade não vai para o plural ("30 dúzia"), um detalhe de `_formatar_quantidade_item` que já existia.

### F3

**Os rascunhos são redigitados pelo LLM em vez de sair das tools. Severidade: média.**

- **Evidência (REL-08):**
  - A tool devolve rascunhos completos, com itens, quantidades, a observação de divisão e "Prazo/Entrega: terça 04/nov".
  - O modelo reescreve todos. Na rodada 1, o rascunho da P001 diz "maracujá/goiaba", mas a P001 só recebeu goiaba (maracujá é da P006). Os demais trocam a quantidade por "conforme alocação".
  - Na rodada 2, o texto afirma que goiaba, alface e rúcula "foram divididos proporcionalmente", mas a tool marca "quantidade a confirmar".
  - Em todas as rodadas, os rascunhos afirmam "entrega em 04/nov", uma data que o próprio sistema sinalizou como inconsistente.
- **Evidência (REL-03, 1 de 3 rodadas):** os dois rascunhos saíram com `\n` literal (barra invertida + n). Como `render_rascunhos` usa `st.code` (`src/sabor/ui/app.py:278`), o card mostra uma linha só, cheia de `\n`.
- **Causa:** `RespostaAgente.rascunhos` vem só do `final_result` do modelo. Diferente de `relatorios` (sobrescritos com `deps.relatorios` em `_anexar_relatorios`), nada amarra os rascunhos ao que a tool devolveu.
- **Correção sugerida:** seguir o padrão dos relatórios. As tools registram seus rascunhos em `deps`, e o runner os anexa de forma determinística, descartando ou ignorando os do modelo quando `resumir_produtor` ou `rascunhar_notificacao` rodaram no turno. Assim o guardrail 4a e a proteção de PII continuam valendo sobre o mesmo conteúdo.

**Status: resolvido.**

- `AgentDeps.rascunhos` recebe os rascunhos que `resumir_produtor` e `rascunhar_notificacao` montam, via `registrar_rascunhos` em `src/sabor/tools/base.py`.
- Na Etapa 4, `_anexar_rascunhos` (`src/sabor/agent/runner.py`) substitui os rascunhos do modelo pelos das tools sempre que uma delas roda no turno, mesmo quando a tool não montou nenhum. Os rascunhos passam pelo mascarador, não têm a PII restaurada e são avaliados pelo guardrail 4a depois.
- Repetições idênticas da mesma chamada viram um rascunho só.
- Quando nenhuma dessas tools roda (por exemplo, numa resposta a um comprador), vale o rascunho do modelo, e o `\n` literal vira quebra de linha.
- O prompt ganhou a diretriz 8: nesses turnos, o modelo deixa `rascunhos` vazio e só aponta o que a equipe precisa conferir.
- **Testes:**
  - em `tests/agent/test_runner.py`, os rascunhos da tool substituem o inventado, saem mascarados e sem duplicata; uma tool sem rascunho descarta o do modelo; o `\n` do modelo é corrigido;
  - nas tools, `resumir_produtor` (em `tests/test_triagem.py`) e `rascunhar_notificacao` registram em `deps`.
- **Validação com o agente:** 3 rodadas de REL-01, REL-03, REL-04 e REL-08, 12 turnos ao todo.
  - Em todos, os rascunhos entregues são exatamente os da tool, sem `\n` literal.
  - No REL-03, a ordem pode variar quando as duas chamadas rodam em paralelo.
  - O texto do REL-08 deixou de inventar a divisão proporcional e só pede que a equipe confira a data e a divisão dos volumes.
- **Efeito colateral corrigido:** com os rascunhos da tool, o guardrail 4a passou a bloquear o REL-08 (3/3) e o REL-04 (1/3). Ver [F10](#f10).

### F4

**`resumir_produtor` vem seguido de tools redundantes quando o pedido diz "relatório". Severidade: média.**

- **Evidência:** no REL-02, as 3 rodadas chamaram `resumir_produtor` e, em seguida, `buscar_documentos`, `consultar_financeiro` e, em 2 rodadas, `buscar_produtores`. A latência foi de 5,5 a 6,3 s, contra cerca de 3 s no REL-01. A regra 7 do prompt ("chame apenas a única ferramenta... use somente `resumir_produtor`") não é seguida.
- **Hipótese:** com `gerar_relatorio` escondido, a palavra "relatório" leva o modelo a "completar" o resumo, por exemplo com DAP/CAF e capacidade (dados corretos, conferidos nas fontes).
- **Correção sugerida:** em `prepare_tools`, depois que `resumir_produtor` roda no turno, deixar só `final_result`, ou só as tools já usadas. Outra opção é explicitar no docstring de `resumir_produtor` que pedidos de "relatório de um produtor" se encerram nela.

**Status: resolvido.**

- **Remedição depois do F1 e do F3:** o REL-02 já não encadeava nada (0/5), mas o REL-01 ainda chamava `buscar_documentos` em 3 de 5 rodadas, sempre para checar a validade da certificação orgânica da P001.
  - A causa: `resumir_produtor` só citava a certificação quando ela estava vencida ou vencia em até 45 dias.
  - O custo: o turno ia de cerca de 9 mil para cerca de 16 mil tokens. Numa das rodadas, estourou o teto de 32 mil (`ORCAMENTO_TURNO_ESGOTADO`), e o operador recebeu "limite atingido". Antes do F1, o REL-02 chegava a 28 mil.
- **Correção:**
  - **Causa:** `ResumoProdutorResultado` passa a trazer sempre `certificacao_organica` (`vigente`, `vencida`, `sem_validade` ou `nao_possui`; nulo quando o produtor não é encontrado), `validade_certificacao` e `registro_sie`. O docstring diz que o resultado já é o panorama completo.
  - **Trava:** `FallbackComCircuitBreaker.prepare_tools` (`src/sabor/agent/fallback.py`) oculta `TOOLS_COBERTAS_PELO_RESUMO` (`buscar_documentos`, `consultar_financeiro`, `buscar_produtores`) no restante do turno depois que `resumir_produtor` roda. `resumir_produtor` continua disponível para outro produtor, e uma consulta que o modelo já pediu no mesmo passo do resumo também.
- **Testes:**
  - `test_prepare_tools_oculta_consultas_cobertas_depois_do_resumo` (inclui um turno novo, para checar que a trava não vaza);
  - `test_prepare_tools_mantem_consulta_ja_chamada_junto_com_o_resumo`;
  - `test_resumo_traz_situacao_da_certificacao_e_sie` (P001 vigente, P008 vencida, P003 sem certificação e com SIE, produtor inexistente).

  Os 3 falham sem a correção.
- **Validação com o agente:** REL-01 a REL-04, 5 rodadas cada, 20 turnos.
  - Todos usaram só `resumir_produtor`, sem bloqueio do guardrail.
  - O REL-01 e o REL-02 ficaram em cerca de 9 mil tokens, e o REL-04 em cerca de 10 mil.
  - No REL-03, o modelo resumiu os dois produtores em passos separados em 3 de 5 rodadas (cerca de 14 mil tokens), e isso continua funcionando.
- **Limitação aceita:** numa pergunta composta, como "resuma o Cícero e diga quanto a SuperBom deve", a consulta extra só acontece se o modelo a pedir no mesmo passo do resumo. Esse caso fica menos importante com o [F5](#f5), que traz no resumo a quebra dos vencidos por comprador.

### F5

**O resumo do produtor não diz quem deve. Severidade: baixa.**

- **Evidência:** o REL-04 mostra "Total vencido: R$ 6.020,00" sem citar a Rede SuperBom (0/3). O PDF espera "R$ 6.020,00 vencidos da SuperBom (FIN-2026-013/014)".
- **Causa:** `ResumoProdutorResultado` só traz totais (`total_pendente`, `total_vencido`), sem quebrar por comprador. O modelo não tem o dado e, corretamente, não o inventa.
- **Correção sugerida:** incluir em `ResumoProdutorResultado` uma lista de vencidos e pendentes com comprador, valor, vencimento e dias de atraso, como `consultar_financeiro` já retorna, e usá-la no texto e no rascunho.

### F6

**A flag `inadimplencia` é aplicada ao produtor credor. Severidade: baixa.**

- **Evidência:** no REL-03 (3/3), "P004 possui valores vencidos na cooperativa"; no REL-04 (2/3), "O produtor possui valores vencidos". Quem está atrasado é o comprador (PNAE e SuperBom), e o produtor é quem tem a receber.
- **Causa:** o prompt (`src/sabor/agent/prompts.py`, diretriz 5) define `inadimplencia` para clientes, mas o modelo a aplica a qualquer valor vencido.
- **Correção sugerida:**
  - Deixar claro no prompt que, no resumo do produtor, o vencido é crédito dele, e que a flag vale para o comprador citado.
  - Ou gerar as flags de forma determinística a partir de `resumir_produtor` (o que combina com [F5](#f5)).

### F7

**O alerta de NC do resumo atribui o laudo municipal à ADAGRO e expõe o caminho interno do arquivo. Severidade: baixa.**

- **Evidência:**
  - O rascunho do REL-04 para o Cícero diz "Não conformidade / apontamento ADAGRO registrado (laudos/laudo_sanitario_queijaria_cicero_P003.txt)".
  - O laudo é da **Vigilância Sanitária Municipal de Caetés**, e a NC-001 é **menor** (lixeira sem pedal, com resultado geral **APROVADO**).
  - A resposta da rodada 3 repetiu "registrado pela ADAGRO".
- **Causa:** `src/sabor/tools/resumir_produtor.py:313` usa um texto fixo com "ADAGRO" para qualquer NC encontrada e inclui o nome do arquivo no alerta, que vai para o rascunho destinado ao produtor.
- **Correção sugerida:**
  - Montar o alerta com o órgão e a severidade do documento de origem, por exemplo: "Laudo VS-CAE-2026-0034: NC-001 (menor), pendente de verificação desde 12/09/2026".
  - Tirar o caminho do arquivo do texto do rascunho; ele já fica em `fontes`.

### F8

**Fontes malformadas. Severidade: baixa.**

- **Evidência:**
  - No REL-08 (3/3), o modelo junta as referências numa só: `produtores.csv · P001, P004, P005, P006, P009, P010, P012`, enquanto a tool devolveu uma fonte por produtor.
  - No REL-07, aparecem `produtores.csv · produtores.csv` (gerada pelo modelo) e `produtores.csv · cadastro` (fixa em `src/sabor/tools/relatorios.py:738`).
  - O REL-03 da rodada 1 cita só FIN-2026-006 para a P004, que também tem a FIN-2026-007.
- **Correção sugerida:** juntar às `fontes` da resposta as fontes retornadas pelas tools (`_extrair_fontes_das_tools` já as coleta para a regra 4b), em vez de depender da lista que o modelo redige.

### F9

**`rascunhar_notificacao` devolve flags sem conteúdo. Severidade: baixa.**

- **Evidência:** as flags da tool vêm como "Sinalização regulatória/operacional: data_inconsistente", "…: ambiguidade" e "…: conformidade", sem dizer qual data, qual item ou qual produtor (`src/sabor/tools/rascunhar_notificacao.py:266`). O modelo acaba redigindo as flags do zero.
- **Correção sugerida:** usar as descrições já calculadas na classificação. `entrega.alerta`, por exemplo, traz o texto completo da divergência entre 04/11 e a terça.

### F10

**O guardrail 4a trata como restritos todos os produtores citados num trecho de inspeção. Severidade: alta. Encontrado ao validar o [F3](#f3).**

- **Evidência:** com os rascunhos determinísticos, o REL-08 foi bloqueado em 3/3 rodadas ("comercialização de item sob restrição sanitária"), e o REL-04 em 1/3 ("produtor sob restrição").
- **Causa:** `_verificar_regra_4a_sanitaria` (`src/sabor/agent/guardrail_exit.py`) avaliava cada trecho de inspeção inteiro. O trecho "ESTABELECIMENTO INSPECIONADO" do relatório da ADAGRO traz P001, P002, P003 e P005 ("REGULAR") junto com P017 ("IRREGULAR"). A palavra "irregular" tornava restritos todos esses produtores e produtos: goiaba, queijo coalho, mel, macaxeira…
- **Por que aparecia agora:** "quantidade a confirmar de goiaba" casa com o padrão comercial `confirmar` e ativava o bloqueio. A versão que o LLM redigitava escapava disso por acaso.
- **Correção:** a restrição passa a ser avaliada por parágrafo, como `_chunk_tem_nc_para_produtor` já faz no resumo. No relatório, cada produtor vistoriado tem um parágrafo próprio, e só o do P017 e o da NC-001 continuam restritos.
- **O que não mudou:**
  - "quantidade a confirmar de frango caipira" segue bloqueado; a correção não afrouxou o padrão comercial.
  - O termo "ovos" também segue restrito, porque aparece no parágrafo do P017 e na NC-001.
- **Testes:** em `tests/agent/test_exit_guardrail.py`, um caso unitário com produtor regular e irregular no mesmo trecho e um caso com o banco real (P001 com goiaba e P003 com queijo passam; P017 com frango é bloqueado). Os dois falham sem a correção.

### Observações (sem ação obrigatória)

- **O resumo sempre traz rascunho.** Quando a pergunta só pede o resumo (REL-02, REL-03 e REL-04), o card de rascunho aparece mesmo assim. Isso é coerente com o docstring de `resumir_produtor`, mas vale decidir se o card deve aparecer só quando o operador pede uma mensagem.
- **Texto impreciso no REL-01.** Na rodada 1, o texto atribui os R$ 1.090 só a "FIN-2026-001 e FIN-2026-002" e omite a FIN-2026-015 (R$ 150), que está nas fontes. O total está correto.
- **Token no dossiê.** O dossiê mostra o remetente da ADAGRO como `[EMAIL_1]`. É o mascaramento esperado no arquivo que sai da cooperativa, mas o token cru no documento pode confundir quem for ler.
- **O que está sólido.** Os três relatórios (REL-05 a REL-07) são determinísticos, corretos e estáveis nas 3 rodadas, sem Markdown colado no texto. O filtro de `gerar_relatorio` por intenção (`mensagem_solicita_relatorio`) funcionou em todos os casos.

## Prioridade sugerida para as correções

1. **[F1](#f1)**: sem ela, o resumo por produtor (Pilar 4.3) nunca mostra demanda. É o que mais aparece numa demo do caso do Cícero.
2. **[F3](#f3)**: rascunhos determinísticos eliminam a invenção de itens e o `\n` literal, e reduzem tokens.
3. **[F2](#f2)**: um item do pedido some sem aviso.
4. **[F4](#f4)** e **[F5](#f5)**: completam o REL-02 e o REL-04 como o PDF espera.
5. **[F6](#f6)** a **[F9](#f9)**: acabamento.
