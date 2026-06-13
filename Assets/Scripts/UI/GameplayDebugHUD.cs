using FarmCreatures.Inventory;
using UnityEngine;

namespace FarmCreatures.UI
{
    public class GameplayDebugHUD : MonoBehaviour
    {
        [SerializeField] private bool showHud = true;

        private void OnGUI()
        {
            if (!showHud)
                return;

            GUILayout.BeginArea(new Rect(10, 10, 360, 220), GUI.skin.box);
            GUILayout.Label("FarmCreatures V0.3 Gameplay");
            GUILayout.Label("WASD / Setas: mover");
            GUILayout.Label("Shift: correr");
            GUILayout.Label("E: interagir/coletar");
            GUILayout.Label("I: imprimir inventário no Console");

            if (InventoryManager.Instance != null)
            {
                GUILayout.Space(8);
                GUILayout.Label("Inventário:");

                foreach (InventorySlot slot in InventoryManager.Instance.Slots)
                {
                    if (slot == null || slot.IsEmpty)
                        continue;

                    GUILayout.Label($"- {slot.item.displayName}: {slot.amount}");
                }
            }

            GUILayout.EndArea();
        }
    }
}
