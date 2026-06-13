FarmCreatures - V0.6 Creature AI + Prefab Flow

Objetivo:
Melhorar o comportamento da criatura nascida e evitar que ela fique presa facilmente na incubadora/árvore.

Conteúdo:
- CreatureController melhorado com detecção de obstáculo.
- CreatureHome para área de movimentação futura.
- CreatureIdlePulse para dar vida visual simples.
- PrototypeTipsHUD.

Como aplicar:
1. Feche o Unity.
2. Extraia este ZIP.
3. Copie a pasta Assets para C:\Projetos\FarmCreatures.
4. Confirme mesclar/substituir.
5. Abra o Unity.

Configuração recomendada:
1. Abra o prefab:
   Assets > Prefabs > Creatures > PFB_Puffin

2. No prefab PFB_Puffin, confirme:
   - Rigidbody2D
   - BoxCollider2D
   - CreatureController
   - CreatureCare
   - CreatureProducer

3. Adicione no prefab:
   - CreatureHome
   - CreatureIdlePulse

4. No CreatureController do PFB_Puffin:
   Obstacle Mask pode ficar Everything por enquanto.

5. Na cena, selecione GameManager e adicione:
   - PrototypeTipsHUD

6. Salve o prefab.

Teste:
- Play.
- Incube ovo.
- Puffin nasce.
- Ele deve andar e trocar rota quando bater/ficar preso.
- Aguarde produção.
- Aperte E perto dele para coletar.

Git:
cd C:\Projetos\FarmCreatures
git add .
git commit -m "V0.6 creature ai prefab flow"
git push
