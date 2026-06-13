using FarmCreatures.Creatures;
using FarmCreatures.Eggs;
using FarmCreatures.Inventory;
using FarmCreatures.World.Interaction;
using UnityEngine;

namespace FarmCreatures.Incubation
{
    public class Incubator : MonoBehaviour, IInteractable
    {
        [Header("Interaction")]
        [SerializeField] private string interactionLabel = "Incubar";

        [Header("Egg")]
        [SerializeField] private EggData acceptedEgg;

        [Header("Spawn")]
        [SerializeField] private Transform hatchSpawnPoint;
        [SerializeField] private GameObject fallbackCreaturePrefab;

        [Header("Visual")]
        [SerializeField] private SpriteRenderer incubatorRenderer;
        [SerializeField] private SpriteRenderer eggRenderer;
        [SerializeField] private Color idleColor = new Color(1f, 0.85f, 0.25f);
        [SerializeField] private Color hatchingColor = new Color(1f, 0.5f, 0.1f);
        [SerializeField] private Color completedColor = new Color(0.4f, 1f, 0.4f);

        private bool isHatching;
        private float hatchTimer;
        private float hatchDuration;
        private EggData currentEgg;

        public string InteractionLabel => interactionLabel;
        public bool IsHatching => isHatching;
        public float Progress01 => hatchDuration <= 0f ? 0f : Mathf.Clamp01(hatchTimer / hatchDuration);
        public float RemainingSeconds => Mathf.Max(0f, hatchDuration - hatchTimer);
        public string CurrentEggName => currentEgg != null ? currentEgg.displayName : "Vazio";

        private void Awake()
        {
            if (incubatorRenderer == null)
                incubatorRenderer = GetComponent<SpriteRenderer>();

            SetVisualIdle();
        }

        private void Update()
        {
            if (!isHatching)
                return;

            hatchTimer += Time.deltaTime;

            if (hatchTimer >= hatchDuration)
                Hatch();
        }

        public void Interact(InteractionContext context)
        {
            if (isHatching)
            {
                Debug.Log($"Incubadora chocando: {RemainingSeconds:0}s restantes.");
                return;
            }

            TryStartHatching();
        }

        public bool TryStartHatching()
        {
            if (acceptedEgg == null || acceptedEgg.eggItem == null)
            {
                Debug.LogWarning("Incubator: acceptedEgg ou eggItem não configurado.");
                return false;
            }

            if (InventoryManager.Instance == null)
            {
                Debug.LogWarning("Incubator: InventoryManager não encontrado.");
                return false;
            }

            if (!InventoryManager.Instance.HasItem(acceptedEgg.eggItem, 1))
            {
                Debug.Log($"Você não possui {acceptedEgg.eggItem.displayName}.");
                return false;
            }

            InventoryManager.Instance.RemoveItem(acceptedEgg.eggItem, 1);

            currentEgg = acceptedEgg;
            hatchDuration = Mathf.Max(1f, acceptedEgg.hatchTimeSeconds);
            hatchTimer = 0f;
            isHatching = true;

            SetVisualHatching();

            Debug.Log($"Incubação iniciada: {currentEgg.displayName}. Tempo: {hatchDuration:0}s.");
            return true;
        }

        private void Hatch()
        {
            isHatching = false;
            hatchTimer = hatchDuration;

            SpawnCreature();
            SetVisualCompleted();

            string creatureName = currentEgg != null && currentEgg.creatureToSpawn != null
                ? currentEgg.creatureToSpawn.displayName
                : "Criatura";

            Debug.Log($"{creatureName} nasceu!");

            currentEgg = null;
        }

        private void SpawnCreature()
        {
            if (currentEgg == null)
                return;

            CreatureData creatureData = currentEgg.creatureToSpawn;
            GameObject prefab = null;

            if (creatureData != null && creatureData.creaturePrefab != null)
                prefab = creatureData.creaturePrefab;
            else
                prefab = fallbackCreaturePrefab;

            Vector3 spawnPosition = hatchSpawnPoint != null
                ? hatchSpawnPoint.position
                : transform.position + Vector3.right * 1.5f;

            GameObject creatureObject;

            if (prefab != null)
            {
                creatureObject = Instantiate(prefab, spawnPosition, Quaternion.identity);
            }
            else
            {
                creatureObject = new GameObject("CRE_Creature_01");
				creatureObject.transform.position = spawnPosition;
				creatureObject.transform.localScale = Vector3.one * 0.7f;

				SpriteRenderer sr = creatureObject.AddComponent<SpriteRenderer>();
				
				sr.color = creatureData != null ? creatureData.bodyColor : Color.green;

				Rigidbody2D rb = creatureObject.AddComponent<Rigidbody2D>();
				rb.gravityScale = 0f;
				rb.freezeRotation = true;

				creatureObject.AddComponent<CreatureController>();
            }

            creatureObject.name = creatureData != null ? $"CRE_{creatureData.displayName}_01" : "CRE_Creature_01";

            SpriteRenderer renderer = creatureObject.GetComponent<SpriteRenderer>();
            if (renderer != null && creatureData != null)
                renderer.color = creatureData.bodyColor;
        }

        private void SetVisualIdle()
        {
            if (incubatorRenderer != null)
                incubatorRenderer.color = idleColor;

            if (eggRenderer != null)
                eggRenderer.enabled = false;
        }

        private void SetVisualHatching()
        {
            if (incubatorRenderer != null)
                incubatorRenderer.color = hatchingColor;

            if (eggRenderer != null)
            {
                eggRenderer.enabled = true;
                eggRenderer.color = currentEgg != null ? currentEgg.eggColor : Color.white;
            }
        }

        private void SetVisualCompleted()
        {
            if (incubatorRenderer != null)
                incubatorRenderer.color = completedColor;

            if (eggRenderer != null)
                eggRenderer.enabled = false;
        }
    }
}
