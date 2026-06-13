using UnityEngine;

namespace FarmCreatures.UI
{
    public class PrototypeTipsHUD : MonoBehaviour
    {
        [SerializeField] private bool show = true;

        private void OnGUI()
        {
            if (!show)
                return;

            GUILayout.BeginArea(new Rect(Screen.width - 310, 10, 300, 145), GUI.skin.box);
            GUILayout.Label("FarmCreatures V0.6");
            GUILayout.Label("Objetivo:");
            GUILayout.Label("- Coletar madeira");
            GUILayout.Label("- Incubar ovo");
            GUILayout.Label("- Puffin nasce pelo prefab");
            GUILayout.Label("- Puffin anda evitando obstáculos");
            GUILayout.Label("- Coletar produção com E");
            GUILayout.EndArea();
        }
    }
}
