using UnityEngine;

namespace FarmCreatures.Creatures
{
    [CreateAssetMenu(fileName = "NewCreatureData", menuName = "FarmCreatures/Creatures/Creature Data")]
    public class CreatureData : ScriptableObject
    {
        [Header("Identity")]
        public string creatureId;
        public string displayName;
        public Sprite icon;

        [Header("Visual")]
        public Color bodyColor = Color.green;

        [Header("Movement")]
        public float moveSpeed = 2f;
        public float wanderRadius = 4f;
        public float idleTime = 2f;

        [Header("Spawn")]
        public GameObject creaturePrefab;
    }
}
