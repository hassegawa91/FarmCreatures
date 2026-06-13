using FarmCreatures.Creatures;
using UnityEngine;

namespace FarmCreatures.World.Spawning
{
    public class CreatureSpawner : MonoBehaviour
    {
        [SerializeField] private CreatureController creaturePrefab;
        [SerializeField] private int amount = 3;
        [SerializeField] private float spawnRadius = 5f;

        private void Start()
        {
            SpawnInitialCreatures();
        }

        public void SpawnInitialCreatures()
        {
            if (creaturePrefab == null)
            {
                Debug.LogWarning("CreatureSpawner: creaturePrefab não configurado.");
                return;
            }

            for (int i = 0; i < amount; i++)
            {
                Vector2 offset = Random.insideUnitCircle * spawnRadius;
                Instantiate(creaturePrefab, (Vector2)transform.position + offset, Quaternion.identity);
            }
        }
    }
}
