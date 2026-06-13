FarmCreatures - V0.2 Estrutural

Direção escolhida:
Mundo livre + Tilemap apenas para terreno.

Como aplicar:
1. Feche o Unity.
2. Extraia este ZIP.
3. Copie a pasta Assets para C:\Projetos\FarmCreatures.
4. Confirme mesclar/substituir.
5. Abra o Unity.
6. Aguarde compilar.

Configuração na cena:
1. Crie um GameObject vazio chamado WorldManager.
2. Adicione:
   - WorldManager
   - WorldBootstrap
   - InteractionManager
3. No Player, adicione:
   - PlayerInteraction
4. Para testar interação:
   - Crie um Square na cena.
   - Adicione BoxCollider2D.
   - Adicione SimpleInteractable.
   - Aperte Play, olhe para o objeto e pressione E.

Depois rode:
cd C:\Projetos\FarmCreatures
git add .
git commit -m "V0.2 arquitetura world interaction spawning"
git push
