using UnityEngine;

namespace FarmCreatures.World.Spawning
{
    public class ResourceSpawner : MonoBehaviour
    {
        [SerializeField] private GameObject resourcePrefab;
        [SerializeField] private int amount = 10;
        [SerializeField] private float spawnRadius = 8f;

        private void Start()
        {
            SpawnResources();
        }

        public void SpawnResources()
        {
            if (resourcePrefab == null)
            {
                Debug.LogWarning("ResourceSpawner: resourcePrefab não configurado.");
                return;
            }

            for (int i = 0; i < amount; i++)
            {
                Vector2 offset = Random.insideUnitCircle * spawnRadius;
                Instantiate(resourcePrefab, (Vector2)transform.position + offset, Quaternion.identity);
            }
        }
    }
}
