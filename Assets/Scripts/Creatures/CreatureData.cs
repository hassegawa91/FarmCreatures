using UnityEngine;

namespace FarmCreatures.Creatures
{
    [CreateAssetMenu(fileName = "NewCreatureData", menuName = "FarmCreatures/Creatures/Creature Data")]
    public class CreatureData : ScriptableObject
    {
        public string creatureId;
        public string displayName;
        public Sprite icon;
        public float moveSpeed = 2f;
        public float wanderRadius = 4f;
        public float idleTime = 2f;
    }
}
