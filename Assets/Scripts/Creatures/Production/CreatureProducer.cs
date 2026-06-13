using FarmCreatures.Creatures.Care;
using FarmCreatures.Inventory;
using FarmCreatures.World.Interaction;
using UnityEngine;

namespace FarmCreatures.Creatures.Production
{
    public class CreatureProducer : MonoBehaviour, IInteractable
    {
        [Header("Production")]
        [SerializeField] private CreatureProductionData productionData;

        [Header("Interaction")]
        [SerializeField] private string readyLabel = "Coletar produção";
        [SerializeField] private string notReadyLabel = "Produzindo";

        [Header("Debug")]
        [SerializeField] private bool autoProduce = true;

        private float timer;
        private bool hasProductReady;
        private CreatureCare care;

        public string InteractionLabel => hasProductReady ? readyLabel : notReadyLabel;
        public bool HasProductReady => hasProductReady;
        public float RemainingSeconds => productionData == null ? 0f : Mathf.Max(0f, productionData.productionTimeSeconds - timer);
        public float Progress01 => productionData == null || productionData.productionTimeSeconds <= 0f ? 0f : Mathf.Clamp01(timer / productionData.productionTimeSeconds);
        public CreatureProductionData ProductionData => productionData;

        private void Awake()
        {
            care = GetComponent<CreatureCare>();
        }

        private void Update()
        {
            if (!autoProduce || hasProductReady || productionData == null)
                return;

            if (productionData.requireCareToProduce && care != null && !care.HasCare(productionData.minimumCare))
                return;

            timer += Time.deltaTime;

            if (timer >= productionData.productionTimeSeconds)
            {
                hasProductReady = true;
                timer = productionData.productionTimeSeconds;
                Debug.Log($"{name} produziu: {productionData.displayName}");
            }
        }

        public void Interact(InteractionContext context)
        {
            if (!hasProductReady)
            {
                Debug.Log($"{name}: ainda produzindo. Restam {RemainingSeconds:0}s.");
                return;
            }

            CollectProduct();
        }

        public void CollectProduct()
        {
            if (productionData == null || productionData.itemProduced == null)
            {
                Debug.LogWarning($"{name}: productionData/itemProduced não configurado.");
                return;
            }

            if (InventoryManager.Instance == null)
            {
                Debug.LogWarning($"{name}: InventoryManager não encontrado.");
                return;
            }

            InventoryManager.Instance.AddItem(productionData.itemProduced, productionData.amount);

            Debug.Log($"Coletado de {name}: {productionData.itemProduced.displayName} x{productionData.amount}");

            hasProductReady = false;
            timer = 0f;
        }
    }
}
