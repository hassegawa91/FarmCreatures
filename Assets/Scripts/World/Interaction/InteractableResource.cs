using FarmCreatures.Inventory;
using UnityEngine;

namespace FarmCreatures.World.Interaction
{
    public class InteractableResource : MonoBehaviour, IInteractable
    {
        [SerializeField] private string interactionLabel = "Coletar";
        [SerializeField] private ItemData itemToGive;
        [SerializeField] private int amount = 1;
        [SerializeField] private bool destroyAfterCollect = true;

        public string InteractionLabel => interactionLabel;

        public void Interact(InteractionContext context)
        {
            if (itemToGive != null && InventoryManager.Instance != null)
            {
                InventoryManager.Instance.AddItem(itemToGive, amount);
                Debug.Log($"Coletado: {itemToGive.displayName} x{amount}");
            }
            else
            {
                Debug.Log($"Recurso coletado: {name}");
            }

            if (destroyAfterCollect)
                Destroy(gameObject);
        }
    }
}
