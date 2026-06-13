using FarmCreatures.Creatures.Production;
using UnityEngine;

namespace FarmCreatures.UI
{
    public class CreatureProductionDebugHUD : MonoBehaviour
    {
        [SerializeField] private bool showHud = true;

        private void OnGUI()
        {
            if (!showHud)
                return;

            CreatureProducer[] producers = FindObjectsByType<CreatureProducer>(FindObjectsSortMode.None);

            GUILayout.BeginArea(new Rect(10, 360, 420, 180), GUI.skin.box);
            GUILayout.Label("Produção das Criaturas");

            if (producers.Length == 0)
            {
                GUILayout.Label("Nenhuma criatura produtora encontrada.");
            }
            else
            {
                foreach (CreatureProducer producer in producers)
                {
                    string productName = producer.ProductionData != null ? producer.ProductionData.displayName : "Sem produção";
                    string status = producer.HasProductReady ? "Pronto!" : $"{producer.RemainingSeconds:0}s";
                    GUILayout.Label($"{producer.name} -> {productName}: {status}");
                    GUILayout.HorizontalSlider(producer.Progress01, 0f, 1f);
                }
            }

            GUILayout.EndArea();
        }
    }
}
