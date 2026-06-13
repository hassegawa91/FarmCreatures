using UnityEngine;

namespace FarmCreatures.Inventory
{
    public class InventoryDebugPrinter : MonoBehaviour
    {
        [SerializeField] private KeyCode printKey = KeyCode.I;

        private void Update()
        {
            if (Input.GetKeyDown(printKey))
                PrintInventory();
        }

        private void PrintInventory()
        {
            if (InventoryManager.Instance == null)
            {
                Debug.LogWarning("InventoryDebugPrinter: InventoryManager não encontrado.");
                return;
            }

            Debug.Log("===== INVENTÁRIO =====");

            foreach (InventorySlot slot in InventoryManager.Instance.Slots)
            {
                if (slot == null || slot.IsEmpty)
                    continue;

                Debug.Log($"{slot.item.displayName} x{slot.amount}");
            }

            Debug.Log("======================");
        }
    }
}
