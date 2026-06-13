using FarmCreatures.Creatures;
using FarmCreatures.Inventory;
using UnityEngine;

namespace FarmCreatures.Eggs
{
    [CreateAssetMenu(fileName = "NewEggData", menuName = "FarmCreatures/Eggs/Egg Data")]
    public class EggData : ScriptableObject
    {
        [Header("Identity")]
        public string eggId;
        public string displayName;

        [Header("Inventory")]
        public ItemData eggItem;

        [Header("Hatching")]
        public float hatchTimeSeconds = 15f;
        public CreatureData creatureToSpawn;

        [Header("Visual")]
        public Color eggColor = Color.white;
    }
}
