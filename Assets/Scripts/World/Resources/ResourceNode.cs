using FarmCreatures.Inventory;
using FarmCreatures.World.Interaction;
using UnityEngine;

namespace FarmCreatures.World.Resources
{
    public class ResourceNode : MonoBehaviour, IInteractable
    {
        [Header("Interaction")]
        [SerializeField] private string interactionLabel = "Coletar";

        [Header("Reward")]
        [SerializeField] private ItemData itemToGive;
        [SerializeField] private int amount = 1;

        [Header("Node")]
        [SerializeField] private bool destroyAfterCollect = true;
        [SerializeField] private int durability = 1;

        public string InteractionLabel => interactionLabel;

        public void Interact(InteractionContext context)
        {
            durability--;

            if (itemToGive != null && InventoryManager.Instance != null)
            {
                InventoryManager.Instance.AddItem(itemToGive, amount);
                Debug.Log($"Coletado: {itemToGive.displayName} x{amount}");
            }
            else
            {
                Debug.Log($"ResourceNode coletado: {name}");
            }

            if (destroyAfterCollect && durability <= 0)
                Destroy(gameObject);
        }
    }
}
