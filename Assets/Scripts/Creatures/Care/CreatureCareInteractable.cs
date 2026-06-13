using FarmCreatures.World.Interaction;
using UnityEngine;

namespace FarmCreatures.Creatures.Care
{
    [RequireComponent(typeof(CreatureCare))]
    public class CreatureCareInteractable : MonoBehaviour, IInteractable
    {
        [SerializeField] private string interactionLabel = "Cuidar";
        [SerializeField] private int careAmount = 10;

        private CreatureCare care;

        public string InteractionLabel => interactionLabel;

        private void Awake()
        {
            care = GetComponent<CreatureCare>();
        }

        public void Interact(InteractionContext context)
        {
            care.AddCare(careAmount);
        }
    }
}
