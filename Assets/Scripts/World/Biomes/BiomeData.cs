using UnityEngine;

namespace FarmCreatures.World.Biomes
{
    [CreateAssetMenu(fileName = "NewBiomeData", menuName = "FarmCreatures/World/Biome Data")]
    public class BiomeData : ScriptableObject
    {
        public string biomeId;
        public string displayName;
        [TextArea] public string description;
    }
}
