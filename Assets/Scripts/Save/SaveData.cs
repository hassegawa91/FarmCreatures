using System;
using UnityEngine;

namespace FarmCreatures.Save
{
    [Serializable]
    public class SaveData
    {
        public string saveVersion = "0.1";
        public Vector3 playerPosition;
        public int day = 1;
        public int coins = 0;
    }
}
