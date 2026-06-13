using UnityEngine;

namespace FarmCreatures.Incubation
{
    public class IncubatorVisualBuilder : MonoBehaviour
    {
        [SerializeField] private bool buildOnAwake = true;

        private void Awake()
        {
            if (buildOnAwake)
                Build();
        }

        public void Build()
        {
            SpriteRenderer rootRenderer = GetComponent<SpriteRenderer>();
            if (rootRenderer == null)
                rootRenderer = gameObject.AddComponent<SpriteRenderer>();

            rootRenderer.color = new Color(1f, 0.85f, 0.25f);
            transform.localScale = new Vector3(1.4f, 1.0f, 1f);

            if (GetComponent<BoxCollider2D>() == null)
                gameObject.AddComponent<BoxCollider2D>();

            Transform egg = transform.Find("EggVisual");
            if (egg == null)
            {
                GameObject eggObject = new GameObject("EggVisual");
                eggObject.transform.SetParent(transform);
                eggObject.transform.localPosition = new Vector3(0f, 0.15f, -0.1f);
                eggObject.transform.localScale = new Vector3(0.45f, 0.65f, 1f);

                SpriteRenderer eggRenderer = eggObject.AddComponent<SpriteRenderer>();
                eggRenderer.color = Color.white;
                eggRenderer.enabled = false;
            }
        }
    }
}
