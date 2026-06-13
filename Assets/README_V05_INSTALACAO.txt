FarmCreatures - V0.5 Creature Production

Objetivo:
Agora a criatura que nasce pode produzir um item com o tempo.

Conteúdo:
- CreatureProductionData
- CreatureProducer
- CreatureCare
- CreatureCareInteractable
- CreatureProductionDebugHUD

Configuração:
1. Criar item produzido:
   Assets > ScriptableObjects > Items
   Create > FarmCreatures > Inventory > Item Data
   Nome: SoftFeather
   itemId: soft_feather
   displayName: Pena Macia
   itemType: CreatureItem

2. Criar ProductionData:
   Assets > ScriptableObjects
   Crie uma pasta chamada Production, se desejar.
   Botão direito > Create > FarmCreatures > Creatures > Production Data
   Nome: PuffinFeatherProduction
   productionId: puffin_feather
   displayName: Pena Macia
   itemProduced: arraste SoftFeather
   amount: 1
   productionTimeSeconds: 20

3. No GameManager:
   Add Component:
   - CreatureProductionDebugHUD

4. Depois que o Puffin nascer:
   Selecione CRE_Puffin_01 na Hierarchy
   Add Component:
   - BoxCollider2D, se ainda não tiver
   - CreatureCare
   - CreatureProducer

   No CreatureProducer:
   productionData = PuffinFeatherProduction

Teste:
- Play
- Incube o ovo
- Aguarde Puffin nascer
- Adicione CreatureProducer no Puffin nascido
- Aguarde 20s
- Aperte E no Puffin para coletar Pena Macia
- Aperte I para ver inventário

Git:
cd C:\Projetos\FarmCreatures
git add .
git commit -m "V0.5 creature production"
git push
