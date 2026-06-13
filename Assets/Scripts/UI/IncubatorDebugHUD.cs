using FarmCreatures.Incubation;
using UnityEngine;

namespace FarmCreatures.UI
{
    public class IncubatorDebugHUD : MonoBehaviour
    {
        [SerializeField] private Incubator incubator;
        [SerializeField] private bool showHud = true;

        private void Start()
        {
            if (incubator == null)
                incubator = FindFirstObjectByType<Incubator>();
        }

        private void OnGUI()
        {
            if (!showHud || incubator == null)
                return;

            GUILayout.BeginArea(new Rect(10, 230, 360, 120), GUI.skin.box);
            GUILayout.Label("Incubadora");

            if (incubator.IsHatching)
            {
                GUILayout.Label($"Ovo: {incubator.CurrentEggName}");
                GUILayout.Label($"Tempo restante: {incubator.RemainingSeconds:0}s");
                GUILayout.HorizontalSlider(incubator.Progress01, 0f, 1f);
            }
            else
            {
                GUILayout.Label("Status: vazia/pronta");
                GUILayout.Label("Aproxime e aperte E para incubar.");
            }

            GUILayout.EndArea();
        }
    }
}
