FarmCreatures - V0.4 Hatch System

Conteúdo:
- EggData
- Incubator
- InitialInventoryGiver
- IncubatorDebugHUD
- IncubatorVisualBuilder
- CreatureData estendido com cor e prefab
- InventoryManager com HasItem e RemoveItem

Como aplicar:
1. Feche o Unity.
2. Extraia este ZIP.
3. Copie a pasta Assets para C:\Projetos\FarmCreatures.
4. Confirme mesclar/substituir.
5. Abra o Unity.
6. Aguarde compilar.

Configuração:
1. Criar ItemData do Ovo:
   Assets > ScriptableObjects > Items
   Create > FarmCreatures > Inventory > Item Data
   Nome: CommonEggItem
   itemId: common_egg
   displayName: Ovo Comum
   itemType: CreatureItem

2. Criar CreatureData:
   Assets > ScriptableObjects > Creatures
   Create > FarmCreatures > Creatures > Creature Data
   Nome: Puffin
   creatureId: puffin
   displayName: Puffin
   bodyColor: escolha uma cor viva

3. Criar EggData:
   Assets > ScriptableObjects > Eggs
   Create > FarmCreatures > Eggs > Egg Data
   Nome: CommonEgg
   eggId: common_egg
   displayName: Ovo Comum
   eggItem: arraste CommonEggItem
   hatchTimeSeconds: 15
   creatureToSpawn: arraste Puffin

4. No GameManager, adicionar:
   - InitialInventoryGiver
   - IncubatorDebugHUD

   Em InitialInventoryGiver:
   initialItem = CommonEggItem
   amount = 1

5. Criar incubadora:
   GameObject > 2D Object > Sprites > Square
   Nome: INC_Basic_01
   Add Component:
   - BoxCollider2D
   - Incubator
   - IncubatorVisualBuilder

   No Incubator:
   acceptedEgg = CommonEgg

6. No Player:
   confirmar que já existe PlayerInteraction.

Teste:
- Play
- Console deve mostrar: Item inicial recebido: Ovo Comum x1
- Vá até INC_Basic_01
- Aperte E
- Incubadora muda de cor e mostra ovo
- Aguarde o timer
- Puffin nasce

Git:
cd C:\Projetos\FarmCreatures
git add .
git commit -m "V0.4 hatch system incubator eggs"
git push
