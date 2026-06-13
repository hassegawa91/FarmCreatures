using UnityEngine;

namespace FarmCreatures.Inventory
{
    public class InitialInventoryGiver : MonoBehaviour
    {
        [SerializeField] private bool giveOnStart = true;
        [SerializeField] private ItemData initialItem;
        [SerializeField] private int amount = 1;

        private void Start()
        {
            if (!giveOnStart)
                return;

            Give();
        }

        public void Give()
        {
            if (initialItem == null)
            {
                Debug.LogWarning("InitialInventoryGiver: initialItem não configurado.");
                return;
            }

            if (InventoryManager.Instance == null)
            {
                Debug.LogWarning("InitialInventoryGiver: InventoryManager não encontrado.");
                return;
            }

            InventoryManager.Instance.AddItem(initialItem, amount);
            Debug.Log($"Item inicial recebido: {initialItem.displayName} x{amount}");
        }
    }
}
