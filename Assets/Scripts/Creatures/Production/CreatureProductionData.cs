using FarmCreatures.Inventory;
using UnityEngine;

namespace FarmCreatures.Creatures.Production
{
    [CreateAssetMenu(fileName = "NewCreatureProductionData", menuName = "FarmCreatures/Creatures/Production Data")]
    public class CreatureProductionData : ScriptableObject
    {
        [Header("Identity")]
        public string productionId;
        public string displayName;

        [Header("Output")]
        public ItemData itemProduced;
        public int amount = 1;

        [Header("Timing")]
        public float productionTimeSeconds = 20f;

        [Header("Rules")]
        public bool requireCareToProduce = false;
        public int minimumCare = 0;
    }
}
