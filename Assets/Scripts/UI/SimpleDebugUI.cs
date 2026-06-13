using UnityEngine;

namespace FarmCreatures.UI
{
    public class SimpleDebugUI : MonoBehaviour
    {
        [SerializeField] private bool showDebug = true;

        private void OnGUI()
        {
            if (!showDebug)
                return;

            GUILayout.BeginArea(new Rect(10, 10, 320, 120), GUI.skin.box);
            GUILayout.Label("FarmCreatures V0.1");
            GUILayout.Label("WASD / Setas: mover personagem");
            GUILayout.Label("Arquitetura inicial ativa");
            GUILayout.EndArea();
        }
    }
}
