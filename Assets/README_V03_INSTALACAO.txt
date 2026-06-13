FarmCreatures - V0.3 Gameplay

Conteúdo:
- ResourceNode: recurso coletável com tecla E.
- InventoryDebugPrinter: imprime inventário com tecla I.
- GameplayDebugHUD: HUD simples na tela.
- CreatureAffinity: afinidade/domesticação.
- CreatureTamingInteractable: interação com criatura selvagem.

Como aplicar:
1. Feche o Unity.
2. Extraia este ZIP.
3. Copie a pasta Assets para C:\Projetos\FarmCreatures.
4. Confirme mesclar/substituir.
5. Abra o Unity.
6. Aguarde compilar.

Configuração rápida:
1. No GameManager, adicione:
   - InventoryDebugPrinter
   - GameplayDebugHUD

2. Crie um ItemData:
   Assets > ScriptableObjects > Items
   Botão direito > Create > FarmCreatures > Inventory > Item Data
   Nome: Wood
   displayName: Madeira
   itemId: wood

3. Crie um Square chamado Tree_Resource:
   - Add Component: BoxCollider2D
   - Add Component: ResourceNode
   - Arraste o ItemData Wood para itemToGive

4. Para criatura:
   Crie um Square chamado Creature_Test
   - Add Rigidbody2D, Gravity Scale 0, Freeze Rotation Z
   - Add CreatureController
   - Add CreatureAffinity
   - Add CreatureTamingInteractable
   - Add BoxCollider2D

Teste:
- WASD move
- E coleta Tree_Resource
- I imprime inventário
- E na Creature_Test aumenta afinidade

Git:
cd C:\Projetos\FarmCreatures
git add .
git commit -m "V0.3 gameplay resource inventory taming"
git push
